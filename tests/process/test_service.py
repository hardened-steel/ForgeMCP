import asyncio
import json
import os
import sys
import shutil
from pathlib import Path

import pytest

from forgemcp.process.service import ProcessService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("inherit", [True, False])
@pytest.mark.parametrize("protocol", [True, False])
async def test_environment_modes(cpp_acceptance_project, monkeypatch, inherit, protocol):
    monkeypatch.setenv("FORGEMCP_PARENT_TEST", "parent")
    processes = ProcessService(cpp_acceptance_project)
    try:
        start = processes.start_protocol if protocol else processes.start
        session = await start(sys.executable, ("-c", "import os,json; print(json.dumps(dict(os.environ)))"),
                              env={"FORGEMCP_CHILD_TEST": "child"}, inherit_environment=inherit)
        if protocol:
            pieces = []
            while (piece := await session.read_stdout()) is not None:
                pieces.append(piece)
            while await session.read_stderr() is not None:
                pass
            output = "".join(pieces)
        else:
            output = "".join([chunk.text async for chunk in session.output() if chunk.stream == "stdout"])
        result = await session.wait()
        assert result.return_code == 0
        environment = json.loads(output)
        assert environment["FORGEMCP_CHILD_TEST"] == "child"
        assert ("FORGEMCP_PARENT_TEST" in environment) == inherit
        assert not processes.details(session.process_id).shell
    finally:
        await processes.close()


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows executable names and shell")
async def test_shell_executable_path_is_literal(cpp_acceptance_project):
    folder = cpp_acceptance_project / "Tools %FORGEMCP_SHELL_TEST% & more"
    folder.mkdir()
    executable = folder / "python.exe"
    shutil.copy2(sys.executable, executable)
    (folder / "pyvenv.cfg").write_text((Path(sys.executable).parent.parent / "pyvenv.cfg").read_text())
    processes = ProcessService(cpp_acceptance_project)
    try:
        session = await processes.start(executable, ("-c", "print(123)"), shell=True,
                                        env={"FORGEMCP_SHELL_TEST": "expanded"}, timeout=5)
        output = "".join([chunk.text async for chunk in session.output() if chunk.stream == "stdout"])
        assert (await session.wait()).return_code == 0
        assert output.strip() == "123"
    finally:
        await processes.close()


@pytest.mark.anyio
@pytest.mark.parametrize("shell", [False, True])
async def test_shell_arguments_and_original_record(cpp_acceptance_project, shell):
    script = cpp_acceptance_project / "echo args.py"
    script.write_text("import json,sys; print(json.dumps(sys.argv[1:]))")
    values = ["hello world", "a&b", "(x)", "", "trailing\\", "a|b", "a>b",
              'quote" & literal', '%FORGEMCP_SHELL_TEST%', "bang!", "caret^", "space \\"]
    processes = ProcessService(cpp_acceptance_project)
    try:
        arguments = (str(script), *values)
        session = await processes.start(sys.executable, arguments, shell=shell, timeout=5,
                                        env={"FORGEMCP_SHELL_TEST": "expanded"})
        output = "".join([chunk.text async for chunk in session.output() if chunk.stream == "stdout"])
        assert (await session.wait()).return_code == 0
        assert json.loads(output) == values
        record = processes.record(session.process_id)
        assert record.arguments == arguments
        assert record.executable == sys.executable
        assert record.shell == shell
        assert processes.overview().processes[0].shell == shell
    finally:
        await processes.close()


@pytest.mark.anyio
async def test_cancel_during_launch_tracks_and_terminates_child(cpp_acceptance_project, monkeypatch):
    created = asyncio.Event()
    release = asyncio.Event()
    original = asyncio.create_subprocess_exec
    async def delayed(*args, **kwargs):
        process = await original(*args, **kwargs)
        created.set()
        await release.wait()
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    processes = ProcessService(cpp_acceptance_project)
    try:
        task = asyncio.create_task(processes.start(sys.executable, ("-c", "import time; time.sleep(30)")))
        await asyncio.wait_for(created.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert len(processes.records) == 1
        assert processes.overview().running == 0
        assert next(iter(processes.records.values())).process.returncode is not None
    finally:
        await processes.close()
