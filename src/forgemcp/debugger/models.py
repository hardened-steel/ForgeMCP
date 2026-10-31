"""Typed debugger options, opaque references, and immutable observation results."""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forgemcp.workspace.path import WorkspacePath


class DebugModel(BaseModel):
    """Reject unknown options and prevent observations being edited in place."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SessionPhase(StrEnum):
    """The lifecycle of the debugger connection."""

    CREATED = "created"
    OPENING = "opening"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class TargetState(StrEnum):
    """The last known execution or dump inspection state."""

    UNKNOWN = "unknown"
    RUNNING = "running"
    STOPPED = "stopped"
    PARTIALLY_STOPPED = "partially_stopped"
    EXITED = "exited"
    DUMP = "dump"


class StepKind(StrEnum):
    """Source-level step operations supported by the shared session contract."""

    OVER = "over"
    INTO = "into"
    OUT = "out"


class DebugTimeouts(DebugModel):
    """Finite deadlines for requests, startup, and transport shutdown."""

    request: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    startup: float = Field(default=45.0, gt=0, allow_inf_nan=False)
    shutdown: float = Field(default=5.0, gt=0, allow_inf_nan=False)


class ExceptionFilter(DebugModel):
    """One adapter-provided exception stop filter."""

    id: str
    label: str
    default: bool = False


class DebugCapabilities(DebugModel):
    """Domain operations supported by the selected adapter and session mode."""

    launch: bool = True
    attach: bool = True
    open_dump: bool = True
    function_breakpoints: bool = False
    exception_info: bool = False
    modules: bool = False
    read_memory: bool = False
    disassemble: bool = False
    variable_paging: bool = False
    exception_filters: list[ExceptionFilter] = Field(default_factory=list)


class StoppedStatus(DebugModel):
    """Details belonging exclusively to the current target stop."""

    kind: Literal["stopped"] = "stopped"
    reason: str
    thread_id: str | None = None
    description: str | None = None
    all_threads_stopped: bool = False


class ExitedStatus(DebugModel):
    """A confirmed exit code from the debuggee, rather than its adapter."""

    kind: Literal["exited"] = "exited"
    exit_code: int


class FailedStatus(DebugModel):
    """A session failure whose target outcome may still be unknown."""

    kind: Literal["failed"] = "failed"
    message: str


class SessionSnapshot(DebugModel):
    """One immutable observation with mutually exclusive terminal or stop details."""

    session_id: str
    mode: Literal["launch", "attach", "dump"] | None = None
    phase: SessionPhase = SessionPhase.CREATED
    target_state: TargetState = TargetState.UNKNOWN
    revision: int = 0
    inspection_generation: int | None = None
    adapter_process_id: int | None = None
    target_pid: int | None = None
    status: (
        Annotated[
            StoppedStatus | ExitedStatus | FailedStatus,
            Field(discriminator="kind"),
        ]
        | None
    ) = None
    capabilities: DebugCapabilities = Field(default_factory=DebugCapabilities)


class SourceMapping(DebugModel):
    """Map a debug-information source prefix to managed local source files."""

    source: str = Field(min_length=1, max_length=4096)
    destination: WorkspacePath


class SourceOptions(DebugModel):
    """Managed symbol directories and explicit source path substitutions."""

    source_mappings: list[SourceMapping] = Field(default_factory=list, max_length=100)
    symbol_directories: list[WorkspacePath] = Field(default_factory=list, max_length=100)


class LaunchOptions(SourceOptions):
    """Launch one managed native executable with literal arguments and environment."""

    executable: WorkspacePath
    arguments: list[str] = Field(default_factory=list, max_length=1024)
    cwd: WorkspacePath = WorkspacePath("project/")
    environment: dict[str, str] = Field(default_factory=dict, max_length=1024)
    stop_on_entry: bool = True

    @field_validator("arguments", "environment")
    @classmethod
    def literal_text(cls, value: list[str] | dict[str, str]) -> list[str] | dict[str, str]:
        """Reject NUL data and invalid environment keys before adapter dispatch."""
        values = value if isinstance(value, list) else [*value.keys(), *value.values()]
        if any("\0" in item or len(item) > 32768 for item in values):
            raise ValueError(
                "Arguments and environment values cannot contain NUL or exceed 32768 characters."
            )
        if isinstance(value, dict) and any(not key or "=" in key for key in value):
            raise ValueError("Environment names must be nonempty and cannot contain '='.")
        return value


class AttachOptions(SourceOptions):
    """Attach to an explicit PID, optionally checking its process birth time."""

    pid: int = Field(gt=0)
    expected_started_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    executable: WorkspacePath | None = None
    stop_on_entry: bool = True


class DumpOptions(SourceOptions):
    """Open a managed user-mode dump and optional matching executable."""

    dump_file: WorkspacePath
    executable: WorkspacePath | None = None


class SourceBreakpoint(DebugModel):
    """A source-line breakpoint without executable expressions."""

    line: int = Field(ge=1)


class SourceBreakpoints(DebugModel):
    """The complete requested breakpoint set for one managed source file."""

    source: WorkspacePath
    points: list[SourceBreakpoint] = Field(default_factory=list, max_length=1000)


class BreakpointPlan(DebugModel):
    """Initial breakpoint configuration applied before program execution starts."""

    sources: list[SourceBreakpoints] = Field(default_factory=list, max_length=100)
    functions: list[str] = Field(default_factory=list, max_length=1000)
    exception_filters: list[str] | None = Field(default=None, max_length=100)


class PageRequest(DebugModel):
    """A bounded half-open collection window."""

    start: int = Field(default=0, ge=0)
    count: int = Field(default=100, ge=1, le=1000)


class StopToken(DebugModel):
    """The inspection generation expected by an execution or stack request."""

    session_id: str
    generation: int = Field(ge=0)


class ThreadRef(DebugModel):
    """A session-local opaque thread identity, valid while that thread exists."""

    session_id: str
    handle: str


class FrameRef(StopToken):
    """An opaque frame handle bound to its suspended generation."""

    handle: str


class VariablesRef(StopToken):
    """An opaque variables container bound to its suspended generation."""

    handle: str


class MemoryRef(StopToken):
    """An opaque memory reference bound to its suspended generation."""

    handle: str


class ExecutionRef(DebugModel):
    """Identify one bounded retained execution outcome."""

    session_id: str
    execution_id: str


class ExecutionReceipt(DebugModel):
    """A command acknowledgement with its independent subsequent-stop reference."""

    execution: ExecutionRef
    snapshot: SessionSnapshot


class WaitResult(DebugModel):
    """An execution outcome or a timeout observation that leaves execution intact."""

    outcome: Literal["stopped", "exited", "closed", "failed", "timeout"]
    snapshot: SessionSnapshot


class CloseResult(DebugModel):
    """Keep physical transport closure distinct from the confirmed target disposition."""

    adapter_closed: bool
    target_outcome: Literal["terminated", "detached", "already_exited", "released", "unknown"]
    error: str | None = None
    snapshot: SessionSnapshot


class Breakpoint(DebugModel):
    """A requested breakpoint and the adapter's actual verified location."""

    requested_line: int | None = None
    line: int | None = None
    verified: bool
    message: str | None = None


class BreakpointsResult(DebugModel):
    """The actual breakpoint state acknowledged for one replacement request."""

    snapshot: SessionSnapshot
    source: WorkspacePath | None = None
    breakpoints: list[Breakpoint]


class ExceptionBreakpointsResult(DebugModel):
    """The acknowledged exception filter selection."""

    snapshot: SessionSnapshot
    filters: list[str]


class Thread(DebugModel):
    """A named thread with its opaque invocation-independent identity."""

    reference: ThreadRef
    name: str


class ThreadsResult(DebugModel):
    """The current bounded thread list."""

    snapshot: SessionSnapshot
    threads: list[Thread]
    truncated: bool = False


class SourceLocation(DebugModel):
    """A source line and optional full-width address without assumed column encoding."""

    path: WorkspacePath | None = None
    line: int | None = None
    address: str | None = None
    source_match: Literal["unverified"] = "unverified"


class StackFrame(DebugModel):
    """One stack frame with a generation-scoped inspection handle."""

    reference: FrameRef
    name: str
    location: SourceLocation
    memory: MemoryRef | None = None


class StackResult(DebugModel):
    """A bounded stack window with adapter-provided total when available."""

    snapshot: SessionSnapshot
    frames: list[StackFrame]
    total: int | None = None
    next_start: int | None = None


class Scope(DebugModel):
    """A named variables container with its expansion cost and optional counts."""

    name: str
    reference: VariablesRef | None = None
    expensive: bool = False
    named_variables: int | None = None
    indexed_variables: int | None = None


class ScopesResult(DebugModel):
    """The bounded set of scopes belonging to one stack frame."""

    snapshot: SessionSnapshot
    scopes: list[Scope]
    truncated: bool = False


class Variable(DebugModel):
    """A rendered value and optional opaque child and memory handles."""

    name: str
    value: str
    type: str | None = None
    reference: VariablesRef | None = None
    memory: MemoryRef | None = None
    named_variables: int | None = None
    indexed_variables: int | None = None


class VariablesResult(DebugModel):
    """A bounded variables window preserving adapter-rendered values as data."""

    snapshot: SessionSnapshot
    variables: list[Variable]
    next_start: int | None = None


class Module(DebugModel):
    """A loaded image and the adapter's available symbol status."""

    name: str
    path: WorkspacePath | None = None
    symbol_status: str | None = None
    address_range: str | None = None


class ModulesResult(DebugModel):
    """A bounded module window with optional total and continuation."""

    snapshot: SessionSnapshot
    modules: list[Module]
    total: int | None = None
    next_start: int | None = None


class ExceptionInfo(DebugModel):
    """Adapter-reported exception details for a stopped thread."""

    snapshot: SessionSnapshot
    exception_id: str
    description: str | None = None
    break_mode: str
    details: str | None = None


class MemoryResult(DebugModel):
    """A bounded base64 memory read, including unavailable bytes in sparse dumps."""

    snapshot: SessionSnapshot
    address: str | None = None
    data: str | None = None
    unreadable_bytes: int = 0


class Instruction(DebugModel):
    """An instruction with exact textual address and optional source location."""

    address: str
    instruction: str
    instruction_bytes: str | None = None
    symbol: str | None = None
    location: SourceLocation | None = None


class DisassemblyResult(DebugModel):
    """A bounded sequence of disassembled instructions."""

    snapshot: SessionSnapshot
    instructions: list[Instruction]
