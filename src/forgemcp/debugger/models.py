"""Immutable transport-neutral public models for launch-only source debugging."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator

from forgemcp.models._base import ForgeModel


class DebuggerState(StrEnum):
    """Lifecycle state of ForgeMCP's single managed debug session."""

    UNAVAILABLE = "unavailable"
    STOPPED = "stopped"
    STARTING = "starting"
    INITIALIZED = "initialized"
    CONFIGURING = "configuring"
    RUNNING = "running"
    PAUSED = "paused"
    TERMINATING = "terminating"
    TERMINATED = "terminated"
    FAILED = "failed"


class DebugStoppedReason(StrEnum):
    """Normalized DAP stop reason without adapter-specific payload."""

    BREAKPOINT = "breakpoint"
    STEP = "step"
    EXCEPTION = "exception"
    PAUSE = "pause"
    ENTRY = "entry"
    FUNCTION_BREAKPOINT = "function_breakpoint"
    DATA_BREAKPOINT = "data_breakpoint"
    INSTRUCTION_BREAKPOINT = "instruction_breakpoint"
    UNKNOWN = "unknown"


class DebugAdapterInfo(ForgeModel):
    """Safe discovery metadata for one built-in backend, never an MCP-supplied path."""

    backend_id: str = Field(min_length=1, max_length=128, description="Stable built-in debugger backend identifier.")
    display_name: str = Field(min_length=1, max_length=256, description="Human-readable installed adapter name.")
    available: bool = Field(description="Whether an exact local adapter candidate is available for strict launch.")
    version: str | None = Field(default=None, max_length=256, description="Previously qualified adapter version, if known.")
    source: str | None = Field(default=None, max_length=256, description="Safe discovery source label, never launch arguments.")
    supported_modes: tuple[str, ...] = Field(default=(), max_length=8, description="ForgeMCP modes intentionally supported by this backend.")
    unavailable_reason: str | None = Field(default=None, max_length=512, description="Safe reason the backend cannot be selected.")


class DebugSessionStatus(ForgeModel):
    """Safe status of the one application-scoped debug session."""

    state: DebuggerState = Field(description="Current debugger state-machine state.")
    backend_id: str | None = Field(default=None, max_length=128, description="Active backend identifier when a session exists.")
    session_generation: int = Field(ge=0, description="Monotonic internal session generation, not a usable handle.")
    stop_generation: int = Field(ge=0, description="Monotonic paused-data generation.")
    capabilities: tuple[str, ...] = Field(default=(), max_length=64, description="Enabled adapter capabilities required by exposed operations.")
    active_thread_id: str | None = Field(default=None, max_length=128, description="Opaque current stopped-thread handle, if available.")
    last_event_sequence: int = Field(ge=0, description="Last assigned event cursor in this session.")
    dropped_event_count: int = Field(ge=0, description="Number of event records evicted from the bounded buffer.")
    debuggee_termination_confirmed: bool = Field(description="Whether launched-debuggee termination was requested and cleanup completed.")
    failure: str | None = Field(default=None, max_length=512, description="Safe terminal failure summary, never adapter output.")


class DebugSource(ForgeModel):
    """Workspace source or intentionally omitted external-source metadata."""

    kind: str = Field(pattern="^(workspace|omitted)$", description="Whether the source is workspace-contained or intentionally omitted.")
    path: str | None = Field(default=None, max_length=4096, description="Workspace-relative path only when kind is workspace.")
    name: str | None = Field(default=None, max_length=1024, description="Bounded adapter display name without external file access.")


class DebugThread(ForgeModel):
    """One stopped thread identified only by an opaque temporary handle."""

    thread_id: str = Field(min_length=16, max_length=128, description="Opaque current-stop thread handle.")
    name: str = Field(min_length=1, max_length=1024, description="Adapter-reported thread display name.")
    is_current: bool = Field(description="Whether this is the stopped thread reported by the adapter.")


class DebugStackFrame(ForgeModel):
    """One paused stack frame with normalized zero-based coordinates."""

    frame_id: str = Field(min_length=16, max_length=128, description="Opaque current-stop stack-frame handle.")
    thread_id: str = Field(min_length=16, max_length=128, description="Opaque thread handle used for this stack trace.")
    index: int = Field(ge=0, description="Zero-based frame index in the returned stack trace.")
    name: str = Field(min_length=1, max_length=4096, description="Adapter-reported frame name.")
    source: DebugSource | None = Field(default=None, description="Workspace source or omitted external source metadata.")
    line: int | None = Field(default=None, ge=0, description="Zero-based source line when a source is available.")
    column: int | None = Field(default=None, ge=0, description="Zero-based source column when available.")


class DebugScope(ForgeModel):
    """A current-frame scope with an opaque variables handle."""

    scope_id: str = Field(min_length=16, max_length=128, description="Opaque current-stop scope handle.")
    name: str = Field(min_length=1, max_length=1024, description="Adapter-reported scope name.")
    expensive: bool = Field(description="Whether the adapter marks scope expansion as expensive.")
    variables_id: str | None = Field(default=None, max_length=128, description="Opaque variables handle when the scope has children.")


class DebugVariable(ForgeModel):
    """A bounded debuggee value intentionally returned to the MCP caller, never logs."""

    name: str = Field(min_length=1, max_length=4096, description="Variable display name.")
    value: str = Field(max_length=16_384, description="Bounded adapter value representation; sensitive and never logged.")
    type: str | None = Field(default=None, max_length=4096, description="Bounded adapter type display text.")
    evaluate_name: str | None = Field(default=None, max_length=4096, description="Adapter expression display text when supplied.")
    variables_id: str | None = Field(default=None, max_length=128, description="Opaque child-variables handle when available.")
    named_variables: int | None = Field(default=None, ge=0, description="Adapter-reported named child count.")
    indexed_variables: int | None = Field(default=None, ge=0, description="Adapter-reported indexed child count.")
    truncated: bool = Field(description="Whether value text was shortened to the public safety limit.")


class DebugBreakpoint(ForgeModel):
    """One source breakpoint with no raw DAP identifier exposed."""

    breakpoint_id: str = Field(min_length=16, max_length=128, description="Opaque breakpoint handle for this source replacement generation.")
    path: str = Field(min_length=1, max_length=4096, description="Workspace-relative requested source path.")
    requested_line: int = Field(ge=0, description="Requested zero-based line.")
    requested_column: int | None = Field(default=None, ge=0, description="Requested zero-based column when supplied.")
    verified: bool = Field(description="Whether the adapter verified this breakpoint.")
    line: int | None = Field(default=None, ge=0, description="Verified zero-based line when reported.")
    column: int | None = Field(default=None, ge=0, description="Verified zero-based column when reported.")
    message: str | None = Field(default=None, max_length=1024, description="Bounded adapter explanation, including normal unverified status.")


class DebugOutputEvent(ForgeModel):
    """Bounded sensitive adapter output retained only in the event ring."""

    sequence: int = Field(ge=1, description="Monotonic event cursor.")
    category: str = Field(min_length=1, max_length=64, description="Normalized adapter output category.")
    output: str = Field(max_length=16_384, description="Bounded debuggee or adapter console output; never logged.")
    truncated: bool = Field(description="Whether output was shortened for event storage.")


class EvaluateResult(ForgeModel):
    """Result of a conservative hover-context inspection expression."""

    result: str = Field(max_length=16_384, description="Bounded evaluation result; sensitive and never logged.")
    type: str | None = Field(default=None, max_length=4096, description="Bounded adapter type display text.")
    variables_id: str | None = Field(default=None, max_length=128, description="Opaque child-variables handle when returned.")
    side_effects_possible: bool = Field(default=True, description="Native expression evaluation may still have debugger-side effects.")
    truncated: bool = Field(description="Whether result text was shortened to the public limit.")


class DebugEvent(ForgeModel):
    """Normalized asynchronous debugger event, never a raw DAP notification."""

    sequence: int = Field(ge=1, description="Monotonic cursor assigned when ForgeMCP receives the event.")
    kind: str = Field(pattern="^(stopped|continued|exited|terminated|output|breakpoint)$", description="Normalized event kind.")
    reason: DebugStoppedReason | None = Field(default=None, description="Normalized stopped reason when kind is stopped.")
    thread_id: str | None = Field(default=None, max_length=128, description="Opaque current-stop thread handle when available.")
    output: DebugOutputEvent | None = Field(default=None, description="Bounded output data when kind is output.")
    exit_code: int | None = Field(default=None, description="Adapter-reported process exit code when kind is exited.")
    description: str | None = Field(default=None, max_length=1024, description="Bounded non-payload event description.")


class DebugEventPage(ForgeModel):
    """Cursor page from the bounded asynchronous debugger event buffer."""

    events: tuple[DebugEvent, ...] = Field(default=(), max_length=256, description="Normalized events newer than the requested cursor.")
    next_cursor: int = Field(ge=0, description="Cursor to pass as after_sequence on the next call.")
    dropped_count: int = Field(ge=0, description="Evicted events before this page due to bounded retention.")
    truncated: bool = Field(description="Whether output or page retention was truncated.")


class DebugBreakpointSpec(ForgeModel):
    """One requested source position in ForgeMCP's zero-based coordinate system."""

    line: int = Field(ge=0, description="Zero-based source line.")
    column: int | None = Field(default=None, ge=0, description="Optional zero-based source column.")


class DebugLaunchRequest(ForgeModel):
    """Deliberately small, transport-neutral launch request accepted by the service."""

    program: str = Field(min_length=1, max_length=4096, description="Workspace-relative executable path.")
    cwd: str | None = Field(default=None, max_length=4096, description="Optional workspace-relative working directory.")
    args: tuple[str, ...] = Field(default=(), max_length=64, description="Bounded individual debuggee arguments, never a shell command.")
    environment: dict[str, str] = Field(default_factory=dict, max_length=32, description="Currently only an empty debuggee environment mapping is permitted.")
    stop_on_entry: bool = Field(default=True, description="Whether to stop at the debuggee entry point by default.")
    initial_breakpoints: dict[str, tuple[DebugBreakpointSpec, ...]] = Field(default_factory=dict, max_length=20, description="Initial source breakpoint sets keyed by workspace-relative path.")

    @field_validator("args")
    @classmethod
    def arguments_are_safe(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not isinstance(value, str) or "\x00" in value or len(value) > 4096 for value in values):
            raise ValueError("Debuggee arguments must be bounded NUL-free strings.")
        return values

