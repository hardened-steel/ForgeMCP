import asyncio
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from forgemcp.process.models import ProcessStatus
from forgemcp.process.service import ProcessService
from forgemcp.toolchain.errors import ToolCommandError, ToolParserError
from forgemcp.toolchain.spec import ToolCommand, ToolKind, ToolSpec, version_parser


def spec_for(code, parser, *, timeout=10):
    return ToolSpec("fixture", ToolKind.OTHER, None, None,
                    {"custom": ToolCommand(lambda **kw: ("-u", "-c", code, *kw.values()),
                                           parser="custom", timeout=timeout)}, {"custom": parser})


@pytest.mark.anyio
async def test_binding_await_and_result_cache(cpp_acceptance_project):
    processes = ProcessService(cpp_acceptance_project)
    spec = spec_for("import os,sys; print(os.environ['TOOLCHAIN_TEST']+sys.argv[1],file=sys.stderr)",
                    version_parser(r"version ([0-9.]+)"))
    environment = {"TOOLCHAIN_TEST": "version "}
    tool = spec.bind(path=Path(sys.executable), processes=processes,
                     environment=environment, inherit_environment=False)
    environment["TOOLCHAIN_TEST"] = "changed"
    try:
        with pytest.raises(ToolCommandError):
            spec.commands["custom"]()
        with pytest.raises(TypeError):
            tool.commands["extra"] = ToolCommand(lambda: ())
        with pytest.raises(FrozenInstanceError):
            tool.path = Path("changed")
        execution = tool.commands["custom"](version="12.34")
        assert not processes.records
        assert await execution == "12.34"
        assert await execution.result() == "12.34"
        assert await execution == "12.34"
        assert execution.parsed_result == "12.34"
        assert execution.process_result.status == ProcessStatus.EXITED
        assert len(processes.records) == 1
        assert spec.path is None and spec.commands["custom"].processes is None
        assert tool.path.is_absolute()
    finally:
        await processes.close()


@pytest.mark.anyio
async def test_streaming_both_pipes_and_final_result_before_process_exit(cpp_acceptance_project):
    async def parse(output, emit):
        streams = set()
        async for chunk in output:
            streams.add(chunk.stream)
            await emit(chunk)
        return streams
    code = "import sys,time; print('first',flush=True); time.sleep(.3); print('second',file=sys.stderr,flush=True); time.sleep(.3)"
    processes = ProcessService(cpp_acceptance_project)
    execution = spec_for(code, parse).bind(path=Path(sys.executable), processes=processes).commands["custom"]()
    try:
        events = []
        async for event in execution:
            events.append(event)
            if len(events) == 1:
                assert execution.process_result is None
                assert processes.overview().running == 1
        assert {event.stream for event in events} == {"stdout", "stderr"}
        assert await execution.result() == {"stdout", "stderr"}
        with pytest.raises(ToolCommandError, match="already"):
            async for event in execution:
                pass
        assert len(processes.records) == 1
    finally:
        await processes.close()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["exit", "parser", "timeout"])
async def test_failures_retain_terminal_state_without_output_in_error(cpp_acceptance_project, failure):
    async def parse(output, emit):
        async for chunk in output:
            await emit("diagnostic")
            if failure == "parser":
                raise ValueError("PRIVATE_TRANSCRIPT")
        return "parsed"
    code = "import sys,time; print('PRIVATE_TRANSCRIPT',flush=True); "
    code += "sys.exit(4)" if failure == "exit" else "time.sleep(5)"
    processes = ProcessService(cpp_acceptance_project)
    execution = spec_for(code, parse, timeout=.4 if failure == "timeout" else 10).bind(
        path=Path(sys.executable), processes=processes).commands["custom"]()
    try:
        with pytest.raises(ToolParserError if failure == "parser" else ToolCommandError) as error:
            async for event in execution:
                assert event == "diagnostic"
        assert "PRIVATE_TRANSCRIPT" not in str(error.value)
        assert execution.process_result is not None
        assert processes.overview().running == 0
        details = processes.details(execution.process_result.process_id)
        assert any("PRIVATE_TRANSCRIPT" in entry.text for entry in details.transcript)
        if failure == "exit":
            assert execution.parsed_result == "parsed"
        with pytest.raises(type(error.value)):
            await execution.result()
    finally:
        await processes.close()


@pytest.mark.anyio
async def test_cancellation_terminates_managed_process(cpp_acceptance_project):
    started = asyncio.Event()
    async def parse(output, emit):
        async for chunk in output:
            started.set()
    processes = ProcessService(cpp_acceptance_project)
    execution = spec_for("import time; print('ready',flush=True); time.sleep(30)", parse).bind(
        path=Path(sys.executable), processes=processes).commands["custom"]()
    try:
        task = asyncio.create_task(execution.result())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert processes.overview().running == 0
        assert execution.process_result.status == ProcessStatus.TERMINATED
    finally:
        await processes.close()


@pytest.mark.anyio
async def test_parser_early_return_drains_output(cpp_acceptance_project):
    async def parse(output, emit):
        async for chunk in output:
            return "early"
    processes = ProcessService(cpp_acceptance_project)
    code = "import sys; print('first',flush=True); [sys.stdout.write('x'*65536) for _ in range(150)]"
    try:
        execution = spec_for(code, parse).bind(path=Path(sys.executable), processes=processes).commands["custom"]()
        assert await asyncio.wait_for(execution.result(), 10) == "early"
        assert execution.process_result.return_code == 0
    finally:
        await processes.close()


@pytest.mark.anyio
async def test_await_only_does_not_block_on_many_events(cpp_acceptance_project):
    async def parse(output, emit):
        async for chunk in output:
            for index in range(1000):
                await emit(index)
        return 1000
    processes = ProcessService(cpp_acceptance_project)
    try:
        execution = spec_for("print('ok')", parse).bind(path=Path(sys.executable), processes=processes).commands["custom"]()
        assert await asyncio.wait_for(execution.result(), 5) == 1000
    finally:
        await processes.close()


@pytest.mark.anyio
async def test_concurrent_event_consumer_rejected_and_iterator_close_cancels(cpp_acceptance_project):
    async def parse(output, emit):
        async for chunk in output:
            for index in range(1000):
                await emit(index)
    processes = ProcessService(cpp_acceptance_project)
    try:
        execution = spec_for("import time; print('ready',flush=True); time.sleep(30)", parse).bind(
            path=Path(sys.executable), processes=processes).commands["custom"]()
        events = execution.events()
        assert await anext(events) == 0
        with pytest.raises(ToolCommandError):
            await anext(execution.events())
        # Closing while the producer is backpressured must not deadlock.
        await asyncio.wait_for(events.aclose(), 5)
        assert processes.overview().running == 0
        assert execution.process_result.status == ProcessStatus.TERMINATED
    finally:
        await processes.close()


def test_keyword_argument_builder_and_bad_arguments(cpp_acceptance_project):
    processes = ProcessService(cpp_acceptance_project)
    spec = ToolSpec("fixture", ToolKind.OTHER, None, None,
                    {"build": ToolCommand(lambda *, target="all": ("--target", target))}, {})
    tool = spec.bind(path=Path(sys.executable), processes=processes)
    assert tool.commands["build"](target="named target").arguments == ("--target", "named target")
    with pytest.raises(ToolCommandError):
        tool.commands["build"](unknown="no")
    with pytest.raises(ToolCommandError):
        tool.commands["build"](target=42)
