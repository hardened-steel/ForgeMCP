"""Constrained standalone LLVM LLDB-DAP backend rules."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import StructuredLogger
from forgemcp.debugger.errors import DebuggerUnavailableError
from forgemcp.debugger.models import DebugAdapterInfo
from forgemcp.processes import LldbDapCandidate, LldbDapQualifier, ProcessRuntime
from forgemcp.toolchain import ToolchainDiscoveryService


class LldbDapBackend:
    """Discovery and safe LLDB-DAP argument construction, not session orchestration."""

    backend_id = "lldb-dap"

    def __init__(
        self, config: ForgeConfig, logger: StructuredLogger, runtime: ProcessRuntime,
        toolchain: ToolchainDiscoveryService | None = None,
    ) -> None:
        self._config = config
        self._logger = logger
        self._runtime = runtime
        self._toolchain = toolchain
        self._candidate: LldbDapCandidate | None = None
        self._info = self._discover_without_starting()

    def discover(self) -> DebugAdapterInfo:
        """Return the fixed local candidate decision without launching a probe."""
        return self._info

    async def start_adapter(self) -> object:
        """Start the exact approved standalone adapter through strict Process Runtime."""
        if self._candidate is None:
            raise DebuggerUnavailableError("No approved standalone lldb-dap adapter is available.")
        handle = await self._runtime.start_trusted_adapter(
            (str(self._candidate.path),),
            approved_path_directories=self._candidate.companion_directories,
        )
        if not handle.required_ownership or not handle.ownership_established:
            await handle.aclose()
            raise DebuggerUnavailableError("Required debug-adapter process-tree ownership was not established.")
        return handle

    def initialize_arguments(self) -> Mapping[str, object]:
        return {
            "clientID": "forgemcp",
            "clientName": "ForgeMCP",
            "adapterID": "lldb",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "supportsRunInTerminalRequest": False,
            "supportsStartDebuggingRequest": False,
        }

    def launch_arguments(
        self,
        *,
        program: str,
        cwd: str,
        args: tuple[str, ...],
        environment: Mapping[str, str],
        stop_on_entry: bool,
    ) -> Mapping[str, object]:
        """Return the sole LLDB-DAP launch surface supported by Phase 1."""
        return {
            "program": program,
            "cwd": cwd,
            "args": list(args),
            "env": dict(environment),
            "stopOnEntry": stop_on_entry,
            "console": "internalConsole",
        }

    def _discover_without_starting(self) -> DebugAdapterInfo:
        """Select one exact non-link candidate by read-only local discovery only."""
        if self._toolchain is not None:
            path = self._toolchain.executable("lldb-dap")
            if path is not None and self._runtime.policy.approves_exact_executable(path):
                self._candidate = LldbDapCandidate(path=path, source=self._toolchain.source("lldb-dap"))
                return DebugAdapterInfo(
                    backend_id=self.backend_id,
                    display_name="LLVM LLDB-DAP",
                    available=True,
                    source=self._toolchain.source("lldb-dap"),
                    supported_modes=("launch",),
                )
            return DebugAdapterInfo(
                backend_id=self.backend_id,
                display_name="LLVM LLDB-DAP",
                available=False,
                supported_modes=("launch",),
                unavailable_reason="No approved standalone lldb-dap executable was discovered.",
            )
        qualifier = LldbDapQualifier(self._config, self._logger)
        for candidate in qualifier.discover():
            path = candidate.path
            try:
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or _is_reparse_point(path)
                    or not self._runtime.policy.approves_exact_executable(path)
                ):
                    continue
                # Keep the lexical path: ProcessPolicy's exact approval must
                # itself reject replacement/link traversal at start time.
                self._candidate = candidate
                return DebugAdapterInfo(
                    backend_id=self.backend_id,
                    display_name="LLVM LLDB-DAP",
                    available=True,
                    source=candidate.source,
                    supported_modes=("launch",),
                )
            except OSError:
                continue
        return DebugAdapterInfo(
            backend_id=self.backend_id,
            display_name="LLVM LLDB-DAP",
            available=False,
            supported_modes=("launch",),
            unavailable_reason="No approved standalone lldb-dap executable was discovered.",
        )


def _is_reparse_point(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & 0x400)
    except (AttributeError, FileNotFoundError, OSError):
        return False
