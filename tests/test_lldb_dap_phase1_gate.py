"""Optional real standalone LLVM LLDB-DAP Phase 1 service gate.

The test is portable: it skips when an explicit or conventional local LLVM
installation cannot compile a Windows DWARF debuggee.  On this development
host the fixed standalone LLVM 22.1.8 installation is present and the gate is
executed by the regression command.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.debugger.backends import LldbDapBackend
from forgemcp.debugger.models import DebugBreakpointSpec, DebugLaunchRequest, DebuggerState
from forgemcp.debugger.service import DebuggerService
from forgemcp.processes import ProcessRuntime
from forgemcp.workspace import WorkspaceService


_DEFAULT_LLDB_DAP = Path(r"C:\Program Files\LLVM\bin\lldb-dap.exe")
_DEFAULT_CLANG = Path(r"C:\Program Files\LLVM\bin\clang.exe")


def _local_llvm_path(variable: str, default: Path) -> Path | None:
    configured = os.environ.get(variable)
    candidate = Path(configured) if configured else default
    return candidate if candidate.is_file() else None


@pytest.mark.skipif(
    _local_llvm_path("FORGEMCP_LLDB_DAP_LIVE_TEST", _DEFAULT_LLDB_DAP) is None
    or _local_llvm_path("FORGEMCP_LLVM_CLANG_LIVE_TEST", _DEFAULT_CLANG) is None,
    reason="real DAP gate requires a local standalone lldb-dap and clang installation",
)
def test_real_lldb_dap_launches_local_pe_dwarf_debuggee_and_cleans_up(tmp_path: Path):
    """Prove the conservative service contract against the installed adapter."""
    adapter = _local_llvm_path("FORGEMCP_LLDB_DAP_LIVE_TEST", _DEFAULT_LLDB_DAP)
    clang = _local_llvm_path("FORGEMCP_LLVM_CLANG_LIVE_TEST", _DEFAULT_CLANG)
    assert adapter is not None and clang is not None
    source = tmp_path / "main.c"
    build = tmp_path / "build"
    build.mkdir()
    executable = build / "dap_phase1.exe"
    source.write_text(
        "int main(void) {\n"
        "  volatile int value = 41;\n"
        "  value += 1;\n"
        "  return value == 42 ? 0 : 1;\n"
        "}\n",
        encoding="utf-8",
    )
    compile_result = subprocess.run(
        [str(clang), "-g", "-gdwarf-4", "-O0", str(source), "-o", str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if compile_result.returncode != 0:
        pytest.skip("local LLVM cannot link the required PE/COFF + DWARF debuggee")
    readobj = clang.with_name("llvm-readobj.exe")
    if readobj.is_file():
        debug_info = subprocess.run(
            [str(readobj), "--sections", str(executable)], capture_output=True, text=True, timeout=15, check=False
        )
        assert ".debug_info" in debug_info.stdout

    async def exercise() -> None:
        config = ForgeConfig(workspace_root=tmp_path, lldb_dap_path=adapter)
        logger = create_logger("CRITICAL")
        runtime = ProcessRuntime(config, logger)
        service = DebuggerService(WorkspaceService(config, logger), runtime, LldbDapBackend(config, logger, runtime))
        assert (await service.list_adapters())[0].available is True
        try:
            launched = await service.launch(
                DebugLaunchRequest(
                    program="build/dap_phase1.exe",
                    cwd="build",
                    stop_on_entry=False,
                    initial_breakpoints={"main.c": (DebugBreakpointSpec(line=2),)},
                )
            )
            adapter_pid = service._handle.pid  # type: ignore[union-attr]  # Test-only ownership observation.
            # LLDB-DAP may emit stopped immediately before the final launch
            # response; the service preserves PAUSED rather than deadlocking.
            assert launched.state in {DebuggerState.CONFIGURING, DebuggerState.RUNNING, DebuggerState.PAUSED}
            for _ in range(100):
                if (await service.status()).state is DebuggerState.PAUSED:
                    break
                await asyncio.sleep(0.05)
            assert (await service.status()).state is DebuggerState.PAUSED
            threads = await service.threads()
            assert threads
            frames = await service.stack_trace(threads[0].thread_id)
            assert frames and frames[0].source is not None and frames[0].source.path == "main.c"
            scopes = await service.scopes(frames[0].frame_id)
            variables = [variable for scope in scopes if scope.variables_id for variable in await service.variables(scope.variables_id)]
            assert any(variable.name == "value" for variable in variables)
            evaluated = await service.evaluate(frames[0].frame_id, "value")
            assert evaluated.result
            await service.step_over(threads[0].thread_id)
            for _ in range(100):
                if (await service.status()).state is DebuggerState.PAUSED:
                    break
                await asyncio.sleep(0.05)
            refreshed_threads = await service.threads()
            await service.continue_execution(refreshed_threads[0].thread_id)
            for _ in range(100):
                if (await service.status()).state in {DebuggerState.TERMINATED, DebuggerState.FAILED}:
                    break
                await asyncio.sleep(0.05)
            assert (await service.events(limit=256)).events
        finally:
            await service.stop()
            await runtime.aclose()
        assert runtime._handles == set()
        if os.name == "nt":
            adapter_check = subprocess.run(
                ["tasklist", "/FI", f"PID eq {adapter_pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            debuggee_check = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {executable.name}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            assert str(adapter_pid) not in adapter_check.stdout
            assert executable.name.casefold() not in debuggee_check.stdout.casefold()

    asyncio.run(exercise())
