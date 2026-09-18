import asyncio
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from forgemcp.process.models import ProcessTimeout
from forgemcp.toolchain.errors import ToolCommandError, ToolParserError
from forgemcp.toolchain.tools import cmake


@pytest.mark.anyio
async def test_ready_spec_environment_and_no_result_cache(scripted_processes):
    processes = scripted_processes
    processes.code = "import os; print('cmake version '+os.environ['TOOLCHAIN_TEST'])"
    tool = cmake.INFO.create_spec(Path(sys.executable), processes, {"TOOLCHAIN_TEST": "12.34"}, False)
    assert not processes.records
    with pytest.raises(FrozenInstanceError):
        tool.path = Path("changed")
    assert await tool.methods["version"]() == "12.34"
    assert await tool.methods["version"]() == "12.34"
    assert len(processes.records) == 2
    assert processes.calls[0][1] == ("--version",)
    assert processes.calls[0][2]["inherit_environment"] is False
    assert processes.calls[0][2]["timeout"].total == 15


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["exit", "parser", "timeout"])
async def test_failures_do_not_expose_transcript(scripted_processes, failure):
    processes = scripted_processes
    processes.code = "import sys,time; print('PRIVATE_TRANSCRIPT',flush=True); "
    processes.code += {"exit": "sys.exit(4)", "parser": "pass", "timeout": "time.sleep(30)"}[failure]
    if failure == "timeout":
        processes.timeout = ProcessTimeout(total=.1)
    tool = cmake.create_spec(Path(sys.executable), processes)
    with pytest.raises(ToolParserError if failure == "parser" else ToolCommandError) as error:
        await tool.methods["version"]()
    assert "PRIVATE_TRANSCRIPT" not in str(error.value)
    assert all(record.status.current_status != "running" for record in processes.records.values())


@pytest.mark.anyio
async def test_valid_banner_does_not_hide_failed_exit(scripted_processes):
    scripted_processes.code = "import sys; print('cmake version 4.1.2'); sys.exit(7)"
    tool = cmake.create_spec(Path(sys.executable), scripted_processes)
    with pytest.raises(ToolCommandError, match="exit 7"):
        await tool.methods["version"]()


@pytest.mark.anyio
async def test_version_drains_both_pipes(scripted_processes):
    scripted_processes.code = "import sys; print('cmake version 4.1.2',flush=True); sys.stdout.write('x'*200000); sys.stderr.write('y'*200000)"
    tool = cmake.create_spec(Path(sys.executable), scripted_processes)
    assert await tool.methods["version"]() == "4.1.2"
    record = next(iter(scripted_processes.records.values()))
    assert record.status.current_status == 0
    assert sum(len(item.text) for item in record.status.transcript if item.stream == "stderr") == 200000


@pytest.mark.anyio
async def test_cancellation_exits_process_context(scripted_processes):
    scripted_processes.code = "import time; time.sleep(30)"
    tool = cmake.create_spec(Path(sys.executable), scripted_processes)
    task = asyncio.create_task(tool.methods["version"]())
    await scripted_processes.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(record.status.current_status == "interrupted" for record in scripted_processes.records.values())
