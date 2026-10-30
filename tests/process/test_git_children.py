"""Process-tree cleanup for Git hooks and SSH helpers retaining inherited pipes."""

import asyncio
from contextlib import nullcontext
import json
import sys

import psutil
import pytest

from forgemcp.process.errors import ProcessError
from forgemcp.process.models import ProcessTimeout
from forgemcp.process.service import ProcessService


@pytest.fixture
def anyio_backend():
    """Use the production asyncio process implementation."""
    return "asyncio"


def alive(pid: int) -> bool:
    """Treat already terminated POSIX zombies as stopped while awaiting OS reaping."""
    try:
        process = psutil.Process(pid)
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@pytest.mark.anyio
@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_stopping_parent_also_stops_its_pipe_holding_child(
    cpp_acceptance_project,
    stop,
):
    """Stop a child helper before inherited stdout can keep the session hanging."""
    service = ProcessService(cpp_acceptance_project)
    child_pid = None
    parent_script = (
        "import json,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print(json.dumps({'child':child.pid}),flush=True); time.sleep(60)"
    )
    try:
        timeout = ProcessTimeout(total=0.75) if stop == "timeout" else ProcessTimeout()
        session = await service.launch(
            sys.executable,
            ["-u", "-c", parent_script],
            timeout=timeout,
        )
        async with asyncio.timeout(8):
            expected = pytest.raises(ProcessError) if stop == "timeout" else nullcontext()
            with expected:
                async with session:
                    await session.close_stdin()
                    output = session.output()
                    text = ""
                    while "\n" not in text:
                        text += (await anext(output)).text
                    child_pid = json.loads(text.splitlines()[0])["child"]
                    assert alive(child_pid)
                    if stop == "cancel":
                        await session.close()
                    else:
                        async for _chunk in output:
                            pass
                        await session.wait()
            while alive(child_pid):
                await asyncio.sleep(0.02)
        assert not alive(child_pid)
    finally:
        await service.close()
        if child_pid is not None and alive(child_pid):
            psutil.Process(child_pid).kill()


@pytest.mark.anyio
async def test_normally_completed_parent_keeps_its_normal_child_output(cpp_acceptance_project):
    """Allow a normally joined helper to complete rather than treating it as cancelled."""
    service = ProcessService(cpp_acceptance_project)
    script = (
        "import subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c',\"print('helper complete')\"]); "
        "child.wait(); print('parent complete')"
    )
    try:
        async with asyncio.timeout(8):
            async with await service.launch(sys.executable, ["-u", "-c", script]) as session:
                await session.close_stdin()
                text = "".join([chunk.text async for chunk in session.output()])
                assert await session.wait() == 0
        assert "helper complete" in text and "parent complete" in text
    finally:
        await service.close()
