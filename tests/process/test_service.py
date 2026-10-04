import asyncio
import json
import os
import sys
import shutil
from pathlib import Path

import pytest

from forgemcp.process.service import ProcessService, ChunkDecoder
from forgemcp.process.models import ProcessEncoding, ProcessTimeout
from forgemcp.process.errors import (
    ProcessError,
    ProcessExitedError,
    ProcessStreamError,
    ProcessStartError,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def processes(cpp_acceptance_project):
    service = ProcessService(cpp_acceptance_project)
    async with asyncio.timeout(15):
        try:
            yield service
        finally:
            await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize("inherit", [True, False])
async def test_environment_modes(processes, monkeypatch, inherit):
    monkeypatch.setenv("FORGEMCP_PARENT_TEST", "parent")
    async with await processes.launch(
        sys.executable,
        ("-c", "import os,json; print(json.dumps(dict(os.environ)))"),
        env={"FORGEMCP_CHILD_TEST": "child"},
        inherit_environment=inherit,
    ) as session:
        output = "".join([chunk.text async for chunk in session.output()])
        assert await session.wait() == 0
    environment = json.loads(output)
    assert environment["FORGEMCP_CHILD_TEST"] == "child"
    assert ("FORGEMCP_PARENT_TEST" in environment) == inherit


@pytest.mark.anyio
async def test_arguments_are_literal_and_retained(processes, cpp_acceptance_project):
    values = [
        "hello world",
        "a&b",
        "(x)",
        "",
        "a|b",
        "a>b",
        'quote" & literal',
        "%FORGEMCP_SHELL_TEST%",
        "bang!",
        "caret^",
    ]
    arguments = ("-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *values)
    async with await processes.launch(sys.executable, arguments) as session:
        output = "".join([chunk.text async for chunk in session.output()])
        assert await session.wait() == 0
        summary = processes.records[session.process_id].summary
        assert summary.arguments == list(arguments)
        assert summary.executable == sys.executable
        assert summary.cwd == str(cpp_acceptance_project.resolve())
    assert json.loads(output) == values


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows executable paths")
async def test_executable_path_is_literal(processes, cpp_acceptance_project):
    folder = cpp_acceptance_project / "Tools %FORGEMCP_SHELL_TEST% & more"
    folder.mkdir()
    executable = folder / "python.exe"
    shutil.copy2(sys.executable, executable)
    (folder / "pyvenv.cfg").write_text(
        (Path(sys.executable).parent.parent / "pyvenv.cfg").read_text()
    )
    async with await processes.launch(executable, ("-c", "print(123)")) as session:
        assert (
            "".join(
                [
                    item.text
                    async for item in session.output()
                    if item.stream == "stdout"
                ]
            ).strip()
            == "123"
        )
        assert await session.wait() == 0


@pytest.mark.anyio
async def test_stdin_fifo_eof_both_outputs_and_repeat_wait(processes):
    code = "import sys; data=sys.stdin.read(); print(data,end=''); print('err',file=sys.stderr)"
    async with await processes.launch(sys.executable, ("-c", code)) as session:
        await session.write_stdin("first")
        await session.write_stdin("second")
        await session.close_stdin()
        await session.close_stdin()
        with pytest.raises(ProcessExitedError):
            await session.write_stdin("late")
        output = {"stdout": "", "stderr": ""}
        async for chunk in session.output():
            output[chunk.stream] += chunk.text
        assert output == {"stdout": "firstsecond", "stderr": "err" + os.linesep}
        assert await session.wait() == await session.wait() == session.returncode == 0
        with pytest.raises(ProcessStreamError):
            await anext(session.output())
    record = processes.records[session.process_id]
    assert record.status.current_status == 0
    assert (
        "".join(
            entry.text for entry in record.transcript if entry.stream == "stdin"
        )
        == "firstsecond"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("send_eof", [False, True])
async def test_stdin_cleanup_after_child_exit(processes, send_eof):
    async with await processes.launch(
        sys.executable,
        ("-c", "print('finished')"),
    ) as session:
        if send_eof:
            await session.close_stdin()
        assert await session.wait() == 0
        assert session.failure is None
        writer = session.process.stdin
        assert writer.is_closing()
        await writer.wait_closed()


@pytest.mark.anyio
async def test_shutdown_with_buffered_stdin_does_not_hang(processes):
    async with await processes.launch(
        sys.executable,
        ("-c", "import time; print('ready', flush=True); time.sleep(30)"),
    ) as session:
        assert (await anext(session.output())).text.strip() == "ready"
        await session.write_stdin("x" * (4 * 1024 * 1024))
        async with asyncio.timeout(5):
            while not session.process.stdin.transport.get_write_buffer_size():
                await asyncio.sleep(0)
            await session.close()
        assert session.task.done()
        assert session.returncode is not None


@pytest.mark.anyio
async def test_wait_drains_large_output_without_consumer(processes):
    code = "import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000); sys.exit(7)"
    async with await processes.launch(sys.executable, ("-c", code)) as session:
        assert await session.wait() == 7
        output = {"stdout": "", "stderr": ""}
        async for chunk in session.output():
            output[chunk.stream] += chunk.text
        assert output == {"stdout": "x" * 200000, "stderr": "y" * 200000}


@pytest.mark.anyio
async def test_cancel_wait_does_not_stop_session(processes):
    async with await processes.launch(
        sys.executable,
        ("-c", "import sys; sys.stdin.read()"),
    ) as session:
        waiter = asyncio.create_task(session.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert session.returncode is None
        assert not session.task.done()
        await session.close_stdin()
        assert await session.wait() == 0


@pytest.mark.anyio
async def test_context_cancellation_stops_workers(processes):
    ready = asyncio.Event()
    sessions = []

    async def owner():
        async with await processes.launch(
            sys.executable,
            ("-c", "import time; time.sleep(30)"),
        ) as session:
            sessions.append(session)
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sessions[0].returncode is not None
    assert sessions[0].task.done()
    await sessions[0].close()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["total", "idle"])
async def test_timeout_reaches_output_and_wait(processes, mode):
    session = await processes.launch(
        sys.executable,
        ("-c", "import time; time.sleep(30)"),
        timeout=ProcessTimeout(**{mode: 0.1}),
    )
    with pytest.raises(ProcessError):
        async with session:
            async for _ in session.output():
                pass
    with pytest.raises(ProcessError):
        await session.wait()
    assert session.returncode is not None
    assert session.task.done()
    assert processes.records[session.process_id].status.current_status == "interrupted"
    snapshot = processes.records[session.process_id].snapshot()
    assert snapshot.status.current_status == "interrupted"
    assert not hasattr(snapshot.status, "transcript")


@pytest.mark.anyio
async def test_decode_failure_is_not_eof(processes):
    with pytest.raises(ProcessStreamError):
        async with await processes.launch(
            sys.executable,
            ("-c", "import sys; sys.stdout.buffer.write(bytes([255]))"),
            encoding=ProcessEncoding("utf-8", errors="strict"),
        ) as session:
            async for _ in session.output():
                pass
    assert session.returncode is not None
    assert processes.records[session.process_id].status.current_status == "stream_failure"
    with pytest.raises(ProcessStreamError):
        await session.wait()


def test_incremental_decoder_and_latin1():
    decoder = ChunkDecoder(ProcessEncoding("utf-8", errors="strict"))
    data = "日本語".encode()
    assert (
        "".join(decoder.decode(bytes([byte])) for byte in data) + decoder.final()
        == "日本語"
    )
    decoder = ChunkDecoder(ProcessEncoding("latin_1", errors="strict"))
    assert decoder.decode(bytes(range(256))).encode("latin_1") == bytes(range(256))


@pytest.mark.anyio
async def test_service_close_stops_all_sessions(processes):
    sessions = [
        await processes.launch(sys.executable, ("-c", "import time; time.sleep(30)"))
        for _ in range(2)
    ]
    await processes.close()
    await processes.close()
    assert all(
        session.returncode is not None and session.task.done() for session in sessions
    )


@pytest.mark.anyio
async def test_invalid_launch_has_no_records(processes, cpp_acceptance_project):
    for kwargs in (
        {"cwd": cpp_acceptance_project.parent},
        {"encoding": ProcessEncoding("invalid-codec")},
    ):
        with pytest.raises(ProcessStartError):
            await processes.launch(sys.executable, **kwargs)
    with pytest.raises(ProcessStartError):
        await processes.launch(cpp_acceptance_project / "missing-program")
    assert not processes.records


@pytest.mark.anyio
async def test_configured_storage_is_an_allowed_working_directory(
    cpp_acceptance_project,
):
    from forgemcp.workspace.service import WorkspaceService

    workspace = WorkspaceService(
        cpp_acceptance_project / "src",
        cpp_acceptance_project / "process-storage",
    )
    directory = workspace.storage_root / "build"
    directory.mkdir(parents=True)
    service = ProcessService(workspace.root, allowed_roots=(workspace.storage_root,))
    try:
        async with await service.launch(
            sys.executable,
            ("-c", "print('storage')"),
            cwd=directory,
        ) as session:
            assert await session.wait() == 0
        with pytest.raises(ProcessStartError):
            await service.launch(sys.executable, cwd=workspace.root.parent)
    finally:
        await service.close()


@pytest.mark.anyio
async def test_cancel_output_does_not_close_session(processes):
    async with await processes.launch(
        sys.executable,
        ("-c", "import sys; sys.stdin.read()"),
    ) as session:
        reader = asyncio.create_task(anext(session.output()))
        await asyncio.sleep(0)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert session.returncode is None
        await session.close_stdin()
        assert await session.wait() == 0


@pytest.mark.anyio
async def test_launch_cancellation_delegates_to_asyncio(processes, monkeypatch):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def launch(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(processes.launch(sys.executable))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert not processes.records


@pytest.mark.anyio
@pytest.mark.parametrize("interval", [0, 1])
async def test_process_overview_uses_shared_progress(processes, monkeypatch, interval):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mcp.server import MCPServer
    from mcp.server.apps import Apps
    from forgemcp.completion import Complete

    processes.progress_interval = interval
    monkeypatch.setattr("forgemcp.progress.monotonic", lambda: 0)
    async with await processes.launch(sys.executable, ["-c", "pass"]) as session:
        await session.wait()
    apps = Apps()
    processes.register(MCPServer("test", extensions=[apps]), apps, Complete())
    handler = apps.tools()[0].fn
    for _ in range(2):
        ctx = SimpleNamespace(report_progress=AsyncMock())
        result = await handler(ctx=ctx)
        assert result.completed == 1
        values = [call.args[0] for call in ctx.report_progress.await_args_list]
        assert values == ([0, 1] if interval == 0 else [0])


@pytest.mark.anyio
async def test_process_inspection_tools_and_markdown_resources(processes):
    from mcp import Client
    from mcp.server import MCPServer
    from mcp.server.apps import Apps
    from forgemcp.completion import Complete

    code = (
        "import sys; "
        "sys.stdout.write('\\x1b[31mred\\x1b[0m'); sys.stdout.flush(); "
        "sys.stderr.write('warning\\n'); sys.stderr.flush()"
    )
    async with await processes.launch(sys.executable, ("-c", code)) as session:
        await session.wait()
    apps = Apps()
    server = MCPServer("test", extensions=[apps])
    processes.register(server, apps, Complete())
    for tool in apps.tools():
        server.add_tool(tool.fn, meta=tool.meta, **tool.kwargs)
    for resource in apps.resources():
        server.add_resource(resource.resource)
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert {"processes_overview", "process_get"} <= tools.keys()
        assert all(
            tools[name].icons and tools[name].meta["ui"]["resourceUri"]
            for name in ("processes_overview", "process_get")
        )
        overview = await client.call_tool("processes_overview", {})
        item = overview.structured_content["processes"][0]
        assert item["summary"]["process_id"] == session.process_id
        assert item["status"]["current_status"] == 0
        assert "outcome" not in item
        assert "transcript" not in item["status"]
        details = await client.call_tool("process_get", {"process_id": session.process_id})
        assert not details.is_error and details.content
        result = details.structured_content
        assert result["process"] == item
        assert result["transcript"]
        assert [
            (entry["stream"], entry["text"]) for entry in result["transcript"]
        ] == [
            (entry.stream, entry.text)
            for entry in processes.records[session.process_id].transcript
        ]
        assert all("elapsed_seconds" not in entry for entry in result["transcript"])
        assert "\x1b[31mred\x1b[0m" in "".join(
            entry["text"] for entry in result["transcript"] if entry["stream"] == "stdout"
        )
        overview_md = (
            await client.read_resource(processes.OVERVIEW_URI)
        ).contents[0].text
        details_md = (
            await client.read_resource(f"forgemcp://processes/{session.process_id}")
        ).contents[0].text
        assert "Completed successfully" in overview_md
        assert "Completed successfully" in details_md
        assert "UTC" in details_md
        assert " · +" in details_md
        assert " s" in details_md
        assert "### stdout" in details_md and "### stderr" in details_md
        assert (await client.call_tool("process_get", {"process_id": 99999})).is_error


@pytest.mark.anyio
async def test_process_snapshot_distinguishes_running_and_nonzero_exit(processes):
    async with await processes.launch(
        sys.executable,
        ("-c", "import sys; sys.stdin.read(); sys.exit(7)"),
    ) as session:
        running = processes.records[session.process_id].snapshot()
        assert running.status.current_status == "running"
        await session.close_stdin()
        assert await session.wait() == 7
    completed = processes.records[session.process_id].snapshot()
    assert completed.status.current_status == 7
