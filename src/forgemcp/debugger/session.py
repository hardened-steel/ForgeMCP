"""The adapter-independent lifecycle and inspection contract for a single target."""

from abc import ABC, abstractmethod

from forgemcp.workspace.path import WorkspacePath

from . import models as m


class DebugSession(ABC):
    """Control one target and return immutable domain observations across calls."""

    @abstractmethod
    def snapshot(self) -> m.SessionSnapshot:
        """Read retained state without performing debugger I/O."""

    @abstractmethod
    async def launch(
        self,
        options: m.LaunchOptions,
        initial: m.BreakpointPlan,
        *,
        timeout: float | None = None,
    ) -> m.ExecutionReceipt:
        """Launch with initial breakpoints applied before execution proceeds."""

    @abstractmethod
    async def attach(
        self,
        options: m.AttachOptions,
        initial: m.BreakpointPlan,
        *,
        timeout: float | None = None,
    ) -> m.ExecutionReceipt:
        """Attach to an explicitly identified existing process."""

    @abstractmethod
    async def open_dump(
        self,
        options: m.DumpOptions,
        *,
        timeout: float | None = None,
    ) -> m.SessionSnapshot:
        """Open an immutable user-mode crash dump for inspection."""

    @abstractmethod
    async def set_breakpoints(
        self,
        source: WorkspacePath,
        points: list[m.SourceBreakpoint],
        *,
        timeout: float | None = None,
    ) -> m.BreakpointsResult:
        """Replace the complete source breakpoint set for one file."""

    @abstractmethod
    async def set_function_breakpoints(
        self,
        functions: list[str],
        *,
        timeout: float | None = None,
    ) -> m.BreakpointsResult:
        """Replace the complete function breakpoint set."""

    @abstractmethod
    async def set_exception_breakpoints(
        self,
        filters: list[str],
        *,
        timeout: float | None = None,
    ) -> m.ExceptionBreakpointsResult:
        """Select only the adapter's advertised exception filters."""

    @abstractmethod
    async def resume(
        self,
        stop: m.StopToken,
        *,
        timeout: float | None = None,
    ) -> m.ExecutionReceipt:
        """Resume execution while invalidating the previous inspection generation."""

    @abstractmethod
    async def step(
        self,
        thread: m.ThreadRef,
        stop: m.StopToken,
        kind: m.StepKind,
        *,
        timeout: float | None = None,
    ) -> m.ExecutionReceipt:
        """Step the selected stopped thread without exposing raw debugger commands."""

    @abstractmethod
    async def pause(self, *, timeout: float | None = None) -> m.ExecutionReceipt:
        """Request a running target to stop."""

    @abstractmethod
    async def wait_for_stop(
        self,
        execution: m.ExecutionRef,
        *,
        timeout: float,
    ) -> m.WaitResult:
        """Wait independently for a command's retained stop or terminal observation."""

    @abstractmethod
    async def threads(self, *, timeout: float | None = None) -> m.ThreadsResult:
        """List bounded named thread identities."""

    @abstractmethod
    async def stack(
        self,
        thread: m.ThreadRef,
        stop: m.StopToken,
        page: m.PageRequest,
        *,
        timeout: float | None = None,
    ) -> m.StackResult:
        """Read a bounded stack page belonging to one stop."""

    @abstractmethod
    async def scopes(
        self,
        frame: m.FrameRef,
        *,
        timeout: float | None = None,
    ) -> m.ScopesResult:
        """Read the frame's named variable containers."""

    @abstractmethod
    async def variables(
        self,
        reference: m.VariablesRef,
        page: m.PageRequest,
        *,
        timeout: float | None = None,
    ) -> m.VariablesResult:
        """Read a bounded page of rendered variables or object children."""

    @abstractmethod
    async def modules(
        self,
        page: m.PageRequest,
        *,
        timeout: float | None = None,
    ) -> m.ModulesResult:
        """Read a bounded loaded-image and symbol-status page."""

    @abstractmethod
    async def exception_info(
        self,
        thread: m.ThreadRef,
        stop: m.StopToken,
        *,
        timeout: float | None = None,
    ) -> m.ExceptionInfo:
        """Inspect the selected thread's current exception information."""

    @abstractmethod
    async def read_memory(
        self,
        reference: m.MemoryRef,
        offset: int,
        count: int,
        *,
        timeout: float | None = None,
    ) -> m.MemoryResult:
        """Read bounded bytes while preserving sparse-dump unavailability."""

    @abstractmethod
    async def disassemble(
        self,
        reference: m.MemoryRef,
        offset: int,
        count: int,
        *,
        timeout: float | None = None,
    ) -> m.DisassemblyResult:
        """Read a bounded instruction sequence at an opaque memory reference."""

    @abstractmethod
    async def close(self, *, timeout: float | None = None) -> m.CloseResult:
        """Close idempotently with target disposition determined by session mode."""
