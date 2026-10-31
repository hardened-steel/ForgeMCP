"""Opt-in end-to-end debugger checks against an installed native LLVM toolchain."""

import os
import ctypes
from ctypes import wintypes

import psutil

import pytest

from forgemcp.debugger import models as m
from forgemcp.debugger.errors import DebugStaleReferenceError
from forgemcp.debugger.lldb_dap import LldbDapSession
from forgemcp.process.models import ProcessTimeout
from forgemcp.toolchain.tools.lldb_dap import create_spec
from forgemcp.workspace.path import WorkspacePath

if os.name == "nt":
    import msvcrt as MSVCRT
else:
    MSVCRT = None


@pytest.fixture
def anyio_backend():
    """Use the production asyncio subprocess and debugger transport."""
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.skipif(
    os.environ.get("FORGEMCP_REAL_DEBUGGER") != "1",
    reason="Set FORGEMCP_REAL_DEBUGGER=1 to build and debug the isolated C++ fixture.",
)
async def test_real_launch_stack_variables_step_and_exit(real_debugger_project):
    """Build DWARF symbols and exercise real stops and generation-scoped inspection."""
    # Keep the real build/debug lifecycle visible as one acceptance scenario.
    # pylint: disable=too-many-locals,too-many-statements
    root, processes, executable, adapter = real_debugger_project
    session = None
    try:
        source = root / "debug" / "debug_main.cpp"
        line = source.read_text(encoding="utf-8").splitlines().index("    return local_value;") + 1
        session = LldbDapSession(
            "real-launch",
            create_spec(adapter, processes),
            project_root=root,
            storage_root=root / ".storage",
        )
        receipt = await session.launch(
            m.LaunchOptions(
                executable=WorkspacePath("project/" + executable.relative_to(root).as_posix()),
                stop_on_entry=False,
            ),
            m.BreakpointPlan(
                sources=[
                    m.SourceBreakpoints(
                        source=WorkspacePath("project/debug/debug_main.cpp"),
                        points=[m.SourceBreakpoint(line=line)],
                    ),
                ],
            ),
        )
        stopped = await session.wait_for_stop(receipt.execution, timeout=20)
        assert stopped.outcome == "stopped", stopped
        stop = m.StopToken(
            session_id="real-launch",
            generation=stopped.snapshot.inspection_generation,
        )
        threads = await session.threads()
        stopped_thread = next(
            item.reference for item in threads.threads
            if item.reference.handle == stopped.snapshot.status.thread_id
        )
        frames = await session.stack(stopped_thread, stop, m.PageRequest())
        assert "debug_leaf" in frames.frames[0].name
        assert len(frames.frames) >= 3
        memory = frames.frames[0].memory
        if memory is not None and stopped.snapshot.capabilities.read_memory:
            assert (await session.read_memory(memory, 0, 16)).data
        if memory is not None and stopped.snapshot.capabilities.disassemble:
            assert (await session.disassemble(memory, 0, 3)).instructions
        scopes = await session.scopes(frames.frames[0].reference)
        local = next(
            item for item in scopes.scopes
            if item.reference is not None and not item.expensive
        )
        variables = await session.variables(local.reference, m.PageRequest())
        assert any(
            item.name == "local_value" and item.value == "42"
            for item in variables.variables
        )
        stepped = await session.step(stopped_thread, stop, m.StepKind.OUT)
        next_stop = await session.wait_for_stop(stepped.execution, timeout=20)
        assert next_stop.outcome == "stopped", next_stop
        with pytest.raises(DebugStaleReferenceError):
            await session.scopes(frames.frames[0].reference)
        resumed = await session.resume(
            m.StopToken(
                session_id="real-launch",
                generation=next_stop.snapshot.inspection_generation,
            ),
        )
        exited = await session.wait_for_stop(resumed.execution, timeout=20)
        assert exited.outcome == "exited", exited
        assert exited.snapshot.status.kind == "exited"
        assert exited.snapshot.status.exit_code == 0
    finally:
        if session is not None:
            await session.close()


def session_for(project, identifier: str) -> LldbDapSession:
    """Bind a fresh concrete driver to the isolated real toolchain fixture."""
    root, processes, _, adapter = project
    return LldbDapSession(
        identifier,
        create_spec(adapter, processes),
        project_root=root,
        storage_root=root / ".storage",
    )


def write_windows_dump(pid: int, path) -> None:
    """Capture a user-mode dump of the stopped fixture without debugger-console commands."""
    if MSVCRT is None:
        pytest.skip("User-mode minidump capture requires Windows.")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    process = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
    if not process:
        raise ctypes.WinError(ctypes.get_last_error())
    writer = ctypes.WinDLL("dbghelp", use_last_error=True).MiniDumpWriteDump
    writer.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    writer.restype = wintypes.BOOL
    try:
        with path.open("wb") as dump:
            if not writer(process, pid, MSVCRT.get_osfhandle(dump.fileno()), 2, None, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(process)


@pytest.mark.anyio
@pytest.mark.skipif(
    os.environ.get("FORGEMCP_REAL_DEBUGGER") != "1",
    reason="Real native debugger acceptance is explicitly opt-in.",
)
async def test_real_attach_close_preserves_external_target(real_debugger_project):
    """Attach to a separately owned fixture and confirm adapter closure leaves it alive."""
    root, processes, executable, _ = real_debugger_project
    session = session_for(real_debugger_project, "real-attach")
    try:
        async with await processes.launch(
            executable,
            ("bounded-running",),
            timeout=ProcessTimeout(total=15),
        ) as target:
            await target.close_stdin()
            pid = target.record.status.pid
            receipt = await session.attach(
                m.AttachOptions(
                    pid=pid,
                    expected_started_at=psutil.Process(pid).create_time(),
                    executable=WorkspacePath("project/" + executable.relative_to(root).as_posix()),
                ),
                m.BreakpointPlan(),
            )
            stopped = await session.wait_for_stop(receipt.execution, timeout=10)
            assert stopped.outcome == "stopped", stopped
            result = await session.close()
            assert result.target_outcome == "detached", result
            assert psutil.Process(pid).is_running()
    finally:
        await session.close()


@pytest.mark.anyio
@pytest.mark.skipif(
    os.environ.get("FORGEMCP_REAL_DEBUGGER") != "1" or os.name != "nt",
    reason="Real Windows minidump capture and LLVM inspection are explicitly opt-in.",
)
async def test_real_user_dump_can_be_reopened_and_inspected(real_debugger_project):
    """Capture the stopped fixture, release its live session, and inspect a real minidump."""
    root, _, executable, _ = real_debugger_project
    live = session_for(real_debugger_project, "dump-source")
    dump = session_for(real_debugger_project, "real-dump")
    try:
        receipt = await live.launch(
            m.LaunchOptions(
                executable=WorkspacePath("project/" + executable.relative_to(root).as_posix()),
            ),
            m.BreakpointPlan(),
        )
        stopped = await live.wait_for_stop(receipt.execution, timeout=10)
        assert stopped.outcome == "stopped", stopped
        dump_file = root / "fixture.dmp"
        write_windows_dump(stopped.snapshot.target_pid, dump_file)
        await live.close()
        snapshot = await dump.open_dump(
            m.DumpOptions(
                dump_file=WorkspacePath("project/fixture.dmp"),
                executable=WorkspacePath("project/" + executable.relative_to(root).as_posix()),
            ),
        )
        assert snapshot.target_state == m.TargetState.DUMP
        threads = await dump.threads()
        assert threads.threads
        frames = await dump.stack(
            threads.threads[0].reference,
            m.StopToken(session_id="real-dump", generation=snapshot.inspection_generation),
            m.PageRequest(),
        )
        assert frames.frames
        closed = await dump.close()
        assert closed.target_outcome == "released", closed
    finally:
        await live.close()
        await dump.close()
