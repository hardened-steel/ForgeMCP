"""Single-session, launch-only DebuggerService built on the DAP client."""

from __future__ import annotations

import asyncio
import contextlib
import re
import secrets
import time
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from forgemcp.dap import (
    DapClient,
    DapConnectionClosedError,
    DapError,
    DapRequestError,
    DapRequestTimeoutError,
)
from forgemcp.dap.errors import DapRequestCompatibility
from forgemcp.debugger.backends.lldb_dap import LldbDapBackend
from forgemcp.debugger.errors import (
    DebuggerFailedError,
    DebuggerHandleExpiredError,
    DebuggerRequestError,
    DebuggerSessionActiveError,
    DebuggerStaleDataError,
    DebuggerStateError,
    DebuggerUnavailableError,
    DebuggerUnsupportedError,
)
from forgemcp.debugger.models import (
    DebugAdapterInfo,
    DebugBreakpoint,
    DebugBreakpointSpec,
    DebugEvent,
    DebugEventPage,
    DebugLaunchRequest,
    DebugOutputEvent,
    DebugScope,
    DebugSessionStatus,
    DebugSource,
    DebugStackFrame,
    DebugStoppedReason,
    DebugThread,
    DebugVariable,
    DebuggerState,
    EvaluateResult,
)
from forgemcp.processes import ProcessHandle, ProcessRuntime
from forgemcp.workspace import WorkspaceError, WorkspaceService

_MAX_EVENT_COUNT = 256
_MAX_EVENT_BYTES = 512 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_MAX_READ_RESULTS = 200
# C++ member access, indexing, dereference, casts, and operator syntax cannot
# be proved side-effect-free: user-defined operators and debugger expression
# evaluation may execute inferior code.  Phase 1 consequently permits only a
# single ASCII identifier lookup in the selected frame.  Even that is labelled
# as side-effect-possible because LLDB owns the evaluation semantics.
_SAFE_EVALUATE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INITIALIZE_CAPABILITIES = ("supportsCancelRequest", "supportsConfigurationDoneRequest", "supportsTerminateRequest")
_STOPPED_REASONS = {
    "breakpoint": DebugStoppedReason.BREAKPOINT,
    "step": DebugStoppedReason.STEP,
    "exception": DebugStoppedReason.EXCEPTION,
    "pause": DebugStoppedReason.PAUSE,
    "entry": DebugStoppedReason.ENTRY,
    "function breakpoint": DebugStoppedReason.FUNCTION_BREAKPOINT,
    "data breakpoint": DebugStoppedReason.DATA_BREAKPOINT,
    "instruction breakpoint": DebugStoppedReason.INSTRUCTION_BREAKPOINT,
}
_CACHE_CAPACITY = {"thread": 256, "frame": 256, "scope": 512, "variables": 2048, "breakpoint": 1024}


class _PauseThreadIdRequired(Exception):
    """Private marker for one qualified LLDB-DAP compatibility response."""


@dataclass(slots=True)
class _HandleRecord:
    kind: str
    native: int
    session_generation: int
    stop_generation: int | None
    expires_at: float


class _HandleCache:
    """Bounded FIFO, monotonic-TTL opaque-handle cache with type isolation."""

    def __init__(self) -> None:
        self._records: dict[str, OrderedDict[str, _HandleRecord]] = {
            kind: OrderedDict() for kind in _CACHE_CAPACITY
        }

    def put(self, kind: str, native: int, session: int, stop: int | None) -> str:
        now = time.monotonic()
        records = self._records[kind]
        self._expire(records, now)
        while len(records) >= _CACHE_CAPACITY[kind]:
            records.popitem(last=False)
        token = secrets.token_urlsafe(32)
        records[token] = _HandleRecord(kind, native, session, stop, now + (120 if stop is not None else 300))
        return token

    def resolve(self, token: str, kind: str, session: int, stop: int | None) -> int:
        now = time.monotonic()
        records = self._records[kind]
        self._expire(records, now)
        record = records.get(token)
        if (
            record is None
            or record.kind != kind
            or record.session_generation != session
            or record.stop_generation != stop
        ):
            raise DebuggerHandleExpiredError("The debugger handle is unknown, expired, stale, or has the wrong type.")
        record.expires_at = now + (120 if stop is not None else 300)
        return record.native

    def clear_stop(self) -> None:
        for kind in ("thread", "frame", "scope", "variables"):
            self._records[kind].clear()

    def clear_all(self) -> None:
        for records in self._records.values():
            records.clear()

    @staticmethod
    def _expire(records: OrderedDict[str, _HandleRecord], now: float) -> None:
        for token, record in tuple(records.items()):
            if record.expires_at <= now:
                del records[token]


class _EventStore:
    """A ring buffer of normalized events with cursor and aggregate byte limits."""

    def __init__(self) -> None:
        self._events: deque[tuple[DebugEvent, int]] = deque()
        self._bytes = 0
        self._next_sequence = 1
        self.dropped_count = 0
        self.truncated = False

    @property
    def last_sequence(self) -> int:
        return self._next_sequence - 1

    @property
    def retained_count(self) -> int:
        return len(self._events)

    def append(self, *, kind: str, **fields: Any) -> DebugEvent:
        event = DebugEvent(sequence=self._next_sequence, kind=kind, **fields)
        self._next_sequence += 1
        size = len(event.model_dump_json().encode("utf-8"))
        while self._events and (len(self._events) >= _MAX_EVENT_COUNT or self._bytes + size > _MAX_EVENT_BYTES):
            _, removed_size = self._events.popleft()
            self._bytes -= removed_size
            self.dropped_count += 1
            self.truncated = True
        if size <= _MAX_EVENT_BYTES:
            self._events.append((event, size))
            self._bytes += size
        else:
            self.dropped_count += 1
            self.truncated = True
        return event

    def page(self, after_sequence: int | None, limit: int) -> DebugEventPage:
        cursor = 0 if after_sequence is None else after_sequence
        if cursor < 0:
            raise DebuggerRequestError("Event cursor must not be negative.")
        oldest = self._events[0][0].sequence if self._events else self._next_sequence
        dropped = self.dropped_count if cursor < oldest - 1 else 0
        events = tuple(event for event, _ in self._events if event.sequence > cursor)[:limit]
        return DebugEventPage(
            events=events,
            next_cursor=self.last_sequence,
            dropped_count=dropped,
            truncated=self.truncated or len(events) == limit and self.last_sequence > cursor + len(events),
        )

    def clear(self) -> None:
        self._events.clear()
        self._bytes = 0
        self._next_sequence = 1
        self.dropped_count = 0
        self.truncated = False


@dataclass(frozen=True, slots=True)
class DebuggerProjectStatusCache:
    """Safe cached debugger metadata with no handles, frames, values, output, or PIDs."""

    adapter: DebugAdapterInfo
    session: DebugSessionStatus
    stop_reason: DebugStoppedReason | None
    thread_count: int | None
    unread_event_count: int
    dropped_event_count: int


class DebuggerService:
    """Own one safe LLDB-DAP launch session per ForgeApplication instance."""

    def __init__(self, workspace: WorkspaceService, process_runtime: ProcessRuntime, backend: LldbDapBackend) -> None:
        self._workspace = workspace
        self._runtime = process_runtime
        self._backend = backend
        self._adapter_info = backend.discover()
        self._state = DebuggerState.STOPPED if self._adapter_info.available else DebuggerState.UNAVAILABLE
        self._session_generation = 0
        self._stop_generation = 0
        self._client: DapClient | None = None
        self._handle: ProcessHandle | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._read_dap_tasks: set[asyncio.Task[Mapping[str, object]]] = set()
        self._handles = _HandleCache()
        self._events = _EventStore()
        self._session_lock = asyncio.Lock()
        self._mutating_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._initialized_event = asyncio.Event()
        self._capabilities: tuple[str, ...] = ()
        self._supports_configuration_done = False
        self._active_thread_id: str | None = None
        self._failure: str | None = None
        self._closing = False
        self._stderr_bytes = 0
        self._debuggee_termination_confirmed = False
        self._breakpoint_generation: dict[str, int] = {}
        self._terminal_event_recorded = False
        self._exited_event_recorded = False
        self._launch_operation: asyncio.Task[object] | None = None
        self._control_operation: asyncio.Task[object] | None = None
        self._last_stop_reason: DebugStoppedReason | None = None
        self._cached_thread_count: int | None = None
        self._last_read_event_sequence = 0

    async def status(self) -> DebugSessionStatus:
        """Return status without launching, probing, or exposing raw protocol data."""
        async with self._state_lock:
            return self._status_locked()

    async def cached_project_status(self) -> DebuggerProjectStatusCache:
        """Return existing state only; never issue a DAP request or discovery probe."""

        async with self._state_lock:
            return DebuggerProjectStatusCache(
                adapter=self._adapter_info,
                session=self._status_locked(),
                stop_reason=self._last_stop_reason,
                thread_count=self._cached_thread_count,
                unread_event_count=max(0, self._events.last_sequence - self._last_read_event_sequence),
                dropped_event_count=self._events.dropped_count,
            )

    async def list_adapters(self) -> tuple[DebugAdapterInfo, ...]:
        """Return read-only backend discovery; it never starts an adapter."""
        return (self._adapter_info,)

    async def events(self, *, after_sequence: int | None = None, limit: int = 100) -> DebugEventPage:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_EVENT_COUNT:
            raise DebuggerRequestError("Event limit must be between 1 and 256.")
        async with self._state_lock:
            page = self._events.page(after_sequence, limit)
            if page.events:
                self._last_read_event_sequence = max(
                    self._last_read_event_sequence, page.events[-1].sequence
                )
            return page

    async def launch(self, request: DebugLaunchRequest) -> DebugSessionStatus:
        """Initialize, configure, and launch one workspace-contained debuggee."""
        async with self._session_lock, self._mutating_lock:
            launch_operation = asyncio.current_task()
            if launch_operation is None:
                raise RuntimeError("Debugger launch requires an asyncio task.")
            if self._state is DebuggerState.UNAVAILABLE:
                raise DebuggerUnavailableError(self._adapter_info.unavailable_reason or "No debugger adapter is available.")
            if (
                self._state in {
                    DebuggerState.STARTING,
                    DebuggerState.INITIALIZED,
                    DebuggerState.CONFIGURING,
                    DebuggerState.RUNNING,
                    DebuggerState.PAUSED,
                    DebuggerState.TERMINATING,
                    DebuggerState.FAILED,
                }
                or self._handle is not None
            ):
                raise DebuggerSessionActiveError("ForgeMCP permits only one active debug session.")
            if request.environment:
                raise DebuggerUnsupportedError("Debuggee environment overrides are not configured for Phase 1.")
            program = self._workspace.validate_execution_path(request.program, kind="file")
            cwd = self._workspace.validate_execution_path(request.cwd or ".", kind="directory")
            launch_task: asyncio.Task[Mapping[str, object]] | None = None
            self._launch_operation = launch_operation
            try:
                await self._begin_launch()
                started = await self._backend.start_adapter()
                if not isinstance(started, ProcessHandle):
                    raise DebuggerUnavailableError("The backend did not return a managed adapter process.")
                self._handle = started
                self._client = DapClient(started.stdout, started.stdin, event_handler=self._on_event)
                await self._client.start()
                self._stderr_task = asyncio.create_task(self._drain_stderr(started), name="forgemcp-debugger-stderr")
                self._watch_task = asyncio.create_task(self._watch_adapter(started), name="forgemcp-debugger-watch")
                initialize = await self._client.request("initialize", self._backend.initialize_arguments(), timeout_seconds=10.0)
                self._set_initialize_capabilities(initialize)
                await self._set_state(DebuggerState.INITIALIZED)
                launch_task = asyncio.create_task(
                    self._client.request(
                        "launch",
                        self._backend.launch_arguments(
                            program=program.native_path,
                            cwd=cwd.native_path,
                            args=request.args,
                            environment={},
                            stop_on_entry=request.stop_on_entry,
                        ),
                        timeout_seconds=20.0,
                    ),
                    name="forgemcp-debugger-launch",
                )
                await asyncio.wait_for(self._initialized_event.wait(), timeout=10.0)
                async with self._state_lock:
                    # A fast adapter may already have delivered ``stopped``
                    # while launch/configuration requests overlap.  Do not
                    # erase that paused state and deadlock configuration.
                    if self._state is DebuggerState.INITIALIZED:
                        self._state = DebuggerState.CONFIGURING
                for path, breakpoints in request.initial_breakpoints.items():
                    await self._set_breakpoints_unlocked(path, breakpoints)
                if self._supports_configuration_done:
                    await self._client.request("configurationDone", {}, timeout_seconds=10.0)
                await launch_task
                async with self._state_lock:
                    if self._state is DebuggerState.CONFIGURING:
                        self._state = DebuggerState.RUNNING
                    return self._status_locked()
            except asyncio.CancelledError:
                if launch_task is not None:
                    launch_task.cancel()
                    await asyncio.gather(launch_task, return_exceptions=True)
                await self._close_unlocked()
                raise
            except Exception as error:
                if launch_task is not None:
                    launch_task.cancel()
                    await asyncio.gather(launch_task, return_exceptions=True)
                await self._mark_failed(error)
                await self._close_unlocked()
                raise self._safe_exception(error) from None
            finally:
                if self._launch_operation is launch_operation:
                    self._launch_operation = None

    async def stop(self) -> DebugSessionStatus:
        """Idempotently terminate the launched debuggee and the strict adapter tree."""
        launch_operation = self._launch_operation
        if launch_operation is not None and launch_operation is not asyncio.current_task() and not launch_operation.done():
            # ``launch`` intentionally holds the session lock across DAP
            # configuration to keep the session slot atomic.  Cancellation is
            # the bounded pre-emption path that lets stop tear down a hung
            # STARTING/CONFIGURING adapter instead of waiting for its timeout.
            launch_operation.cancel()
        control_operation = self._control_operation
        if control_operation is not None and control_operation is not asyncio.current_task() and not control_operation.done():
            # In particular, stop can pre-empt the bounded Windows pause
            # compatibility retry instead of waiting for both DAP deadlines.
            control_operation.cancel()
            await asyncio.gather(control_operation, return_exceptions=True)
        async with self._session_lock, self._mutating_lock:
            await self._close_unlocked()
            return await self.status()

    async def aclose(self) -> None:
        """Plugin lifecycle shutdown; completes before ProcessRuntime shutdown."""
        await self.stop()
        async with self._state_lock:
            # Terminal events remain queryable after debugger__stop, but not
            # after whole-application shutdown and never into a new session.
            self._events.clear()

    async def set_breakpoints(self, path: str, breakpoints: tuple[DebugBreakpointSpec, ...]) -> tuple[DebugBreakpoint, ...]:
        async with self._mutating_lock:
            self._require_state(DebuggerState.CONFIGURING, DebuggerState.RUNNING, DebuggerState.PAUSED)
            return await self._set_breakpoints_unlocked(path, breakpoints)

    async def continue_execution(self, thread_id: str | None = None) -> DebugSessionStatus:
        return await self._execution_request("continue", thread_id)

    async def pause(self, thread_id: str | None = None) -> DebugSessionStatus:
        operation = asyncio.current_task()
        if operation is not None:
            self._control_operation = operation
        try:
            async with self._mutating_lock:
                self._require_state(DebuggerState.RUNNING)
                session_generation = self._session_generation
                native = None if thread_id is None else self._resolve(thread_id, "thread")
                try:
                    await self._request("pause", {} if native is None else {"threadId": native})
                except _PauseThreadIdRequired:
                    # LLDB-DAP on Windows requires a threadId despite the DAP
                    # schema making it optional. Obtain the ID from this exact
                    # live session, retry once, and never create/expose a handle.
                    if native is not None:
                        raise DebuggerRequestError(
                            "The debug adapter rejected the requested operation."
                        ) from None
                    self._require_state(DebuggerState.RUNNING)
                    body = await self._request("threads", {})
                    if self._session_generation != session_generation:
                        raise DebuggerRequestError(
                            "The debug session changed before pause could be retried."
                        )
                    threads = body.get("threads")
                    native = next(
                        (
                            int(item["id"])
                            for item in threads[:256]
                            if isinstance(item, Mapping) and _positive_int(item.get("id"))
                        ),
                        None,
                    ) if isinstance(threads, list) else None
                    if native is None:
                        raise DebuggerRequestError("The debug adapter returned no pausable thread.") from None
                    self._require_state(DebuggerState.RUNNING)
                    await self._request("pause", {"threadId": native})
                return await self.status()
        finally:
            if self._control_operation is operation:
                self._control_operation = None

    async def step_over(self, thread_id: str) -> DebugSessionStatus:
        return await self._execution_request("next", thread_id)

    async def step_in(self, thread_id: str) -> DebugSessionStatus:
        return await self._execution_request("stepIn", thread_id)

    async def step_out(self, thread_id: str) -> DebugSessionStatus:
        return await self._execution_request("stepOut", thread_id)

    async def threads(self) -> tuple[DebugThread, ...]:
        generation = self._require_paused()
        body = await self._read_request("threads", {}, generation)
        threads = body.get("threads")
        if not isinstance(threads, list):
            raise DebuggerRequestError("The debug adapter returned invalid thread data.")
        result: list[DebugThread] = []
        for item in threads[:256]:
            if not isinstance(item, Mapping) or not _positive_int(item.get("id")):
                continue
            token = self._handles.put("thread", int(item["id"]), self._session_generation, generation)
            current = item.get("id") == self._current_native_thread_id()
            if current:
                self._active_thread_id = token
            result.append(DebugThread(thread_id=token, name=_text(item.get("name"), "Thread", 1024)[0], is_current=current))
        self._check_stop_generation(generation)
        self._cached_thread_count = len(result)
        return tuple(result)

    async def stack_trace(self, thread_id: str, *, start_frame: int = 0, levels: int = 100) -> tuple[DebugStackFrame, ...]:
        if not 0 <= start_frame <= 10_000 or not 1 <= levels <= 100:
            raise DebuggerRequestError("Stack-frame paging is outside the supported bound.")
        generation = self._require_paused()
        native_thread = self._resolve(thread_id, "thread")
        body = await self._read_request("stackTrace", {"threadId": native_thread, "startFrame": start_frame, "levels": levels}, generation)
        frames = body.get("stackFrames")
        if not isinstance(frames, list):
            raise DebuggerRequestError("The debug adapter returned invalid stack-frame data.")
        result: list[DebugStackFrame] = []
        for index, item in enumerate(frames[:levels]):
            if not isinstance(item, Mapping) or not _positive_int(item.get("id")):
                continue
            frame_id = self._handles.put("frame", int(item["id"]), self._session_generation, generation)
            source = self._normalize_source(item.get("source"))
            line = _dap_coordinate(item.get("line"))
            column = _dap_coordinate(item.get("column"))
            result.append(DebugStackFrame(frame_id=frame_id, thread_id=thread_id, index=start_frame + index, name=_text(item.get("name"), "<frame>", 4096)[0], source=source, line=line, column=column))
        self._check_stop_generation(generation)
        return tuple(result)

    async def scopes(self, frame_id: str) -> tuple[DebugScope, ...]:
        generation = self._require_paused()
        native_frame = self._resolve(frame_id, "frame")
        body = await self._read_request("scopes", {"frameId": native_frame}, generation)
        scopes = body.get("scopes")
        if not isinstance(scopes, list):
            raise DebuggerRequestError("The debug adapter returned invalid scope data.")
        result: list[DebugScope] = []
        for item in scopes[:64]:
            if not isinstance(item, Mapping):
                continue
            reference = item.get("variablesReference")
            variables_id = self._handles.put("variables", int(reference), self._session_generation, generation) if _positive_int(reference) else None
            scope_id = self._handles.put("scope", int(reference), self._session_generation, generation) if _positive_int(reference) else self._handles.put("scope", 0, self._session_generation, generation)
            result.append(DebugScope(scope_id=scope_id, name=_text(item.get("name"), "Scope", 1024)[0], expensive=item.get("expensive") is True, variables_id=variables_id))
        self._check_stop_generation(generation)
        return tuple(result)

    async def variables(self, variables_id: str, *, start: int = 0, count: int = 100) -> tuple[DebugVariable, ...]:
        if not 0 <= start <= 100_000 or not 1 <= count <= _MAX_READ_RESULTS:
            raise DebuggerRequestError("Variable paging is outside the supported bound.")
        generation = self._require_paused()
        reference = self._resolve(variables_id, "variables")
        body = await self._read_request("variables", {"variablesReference": reference, "start": start, "count": count}, generation)
        values = body.get("variables")
        if not isinstance(values, list):
            raise DebuggerRequestError("The debug adapter returned invalid variable data.")
        result = tuple(self._normalize_variable(item, generation) for item in values[:count] if isinstance(item, Mapping))
        self._check_stop_generation(generation)
        return result

    async def evaluate(self, frame_id: str, expression: str) -> EvaluateResult:
        if not isinstance(expression, str) or not expression or len(expression) > 1024 or not _SAFE_EVALUATE.fullmatch(expression):
            raise DebuggerUnsupportedError("Evaluate accepts only one ASCII identifier for hover lookup; native evaluation may still have side effects.")
        generation = self._require_paused()
        native_frame = self._resolve(frame_id, "frame")
        body = await self._read_request("evaluate", {"expression": expression, "frameId": native_frame, "context": "hover"}, generation)
        result, truncated = _text(body.get("result"), "", 16_384)
        reference = body.get("variablesReference")
        variables_id = self._handles.put("variables", int(reference), self._session_generation, generation) if _positive_int(reference) else None
        self._check_stop_generation(generation)
        return EvaluateResult(result=result, type=_text_or_none(body.get("type"), 4096), variables_id=variables_id, truncated=truncated)

    async def _begin_launch(self) -> None:
        self._session_generation += 1
        self._stop_generation = 0
        self._handles.clear_all()
        self._events.clear()
        self._initialized_event = asyncio.Event()
        self._capabilities = ()
        self._supports_configuration_done = False
        self._active_thread_id = None
        self._failure = None
        self._closing = False
        self._stderr_bytes = 0
        self._debuggee_termination_confirmed = False
        self._breakpoint_generation.clear()
        self._terminal_event_recorded = False
        self._exited_event_recorded = False
        await self._set_state(DebuggerState.STARTING)

    async def _set_breakpoints_unlocked(self, path: str, breakpoints: tuple[DebugBreakpointSpec, ...]) -> tuple[DebugBreakpoint, ...]:
        if len(breakpoints) > 100:
            raise DebuggerRequestError("A source breakpoint set may contain at most 100 positions.")
        source = self._workspace.validate_execution_path(path, kind="file")
        self._breakpoint_generation[path] = self._breakpoint_generation.get(path, 0) + 1
        arguments: dict[str, object] = {
            "source": {"path": source.native_path},
            "breakpoints": [
                {"line": item.line + 1, **({"column": item.column + 1} if item.column is not None else {})}
                for item in breakpoints
            ],
        }
        body = await self._request("setBreakpoints", arguments)
        response = body.get("breakpoints")
        if not isinstance(response, list):
            raise DebuggerRequestError("The debug adapter returned invalid breakpoint data.")
        result: list[DebugBreakpoint] = []
        for index, requested in enumerate(breakpoints):
            item = response[index] if index < len(response) and isinstance(response[index], Mapping) else {}
            native = item.get("id") if isinstance(item, Mapping) else None
            token = self._handles.put("breakpoint", int(native) if _positive_int(native) else index, self._session_generation, None)
            result.append(DebugBreakpoint(
                breakpoint_id=token,
                path=source.relative_path,
                requested_line=requested.line,
                requested_column=requested.column,
                verified=item.get("verified") is True,
                line=_dap_coordinate(item.get("line")),
                column=_dap_coordinate(item.get("column")),
                message=_text_or_none(item.get("message"), 1024),
            ))
        return tuple(result)

    async def _execution_request(self, command: str, thread_id: str | None) -> DebugSessionStatus:
        async with self._mutating_lock:
            self._require_state(DebuggerState.PAUSED)
            stop_generation = self._stop_generation
            native = self._resolve(thread_id, "thread") if thread_id is not None else None
            await self._invalidate_stopped_data()
            await self._request(command, {} if native is None else {"threadId": native})
            async with self._state_lock:
                # A fast adapter may deliver continued + stopped before its
                # execution response. Never overwrite that newer stop (or a
                # terminal/failure event) with a stale RUNNING transition.
                if (
                    self._stop_generation == stop_generation
                    and self._state
                    not in {
                        DebuggerState.TERMINATING,
                        DebuggerState.TERMINATED,
                        DebuggerState.FAILED,
                    }
                ):
                    self._state = DebuggerState.RUNNING
            return await self.status()

    async def _request(self, command: str, arguments: Mapping[str, object]) -> Mapping[str, object]:
        if self._client is None:
            raise DebuggerFailedError("The debug adapter transport is not available.")
        try:
            return await self._client.request(command, arguments)
        except DapRequestTimeoutError as error:
            raise DebuggerRequestError("The debug adapter request timed out.") from error
        except DapRequestError as error:
            if self._pause_requires_thread_id(command, arguments, error):
                raise _PauseThreadIdRequired from error
            raise DebuggerRequestError("The debug adapter rejected the requested operation.") from error
        except DapError as error:
            await self._mark_failed(error)
            raise DebuggerFailedError("The debug-adapter transport failed.") from error

    def _pause_requires_thread_id(
        self, command: str, arguments: Mapping[str, object], error: DapRequestError
    ) -> bool:
        """Recognize only the bounded known LLDB-DAP missing-thread response."""
        if (
            self._backend.backend_id != "lldb-dap"
            or command != "pause"
            or arguments
            or error.command != "pause"
        ):
            return False
        return error.compatibility is DapRequestCompatibility.PAUSE_THREAD_ID_REQUIRED

    async def _read_request(self, command: str, arguments: Mapping[str, object], generation: int) -> Mapping[str, object]:
        if self._client is None:
            raise DebuggerFailedError("The debug adapter transport is not available.")
        task = asyncio.create_task(self._request(command, arguments), name=f"forgemcp-debugger-{command}")
        self._read_dap_tasks.add(task)
        try:
            body = await task
        except asyncio.CancelledError as error:
            raise DebuggerStaleDataError("Paused debugger data became stale while the request was pending.") from error
        finally:
            self._read_dap_tasks.discard(task)
        self._check_stop_generation(generation)
        return body

    async def _on_event(self, event: str, body: Mapping[str, object]) -> None:
        """Receive events without taking the mutating lock or retaining raw payloads."""
        async with self._state_lock:
            if event == "initialized":
                self._initialized_event.set()
            elif event == "stopped":
                await self._invalidate_stopped_data_locked()
                self._stop_generation += 1
                native_thread = body.get("threadId")
                self._active_thread_id = self._handles.put("thread", int(native_thread), self._session_generation, self._stop_generation) if _positive_int(native_thread) else None
                reason = _STOPPED_REASONS.get(str(body.get("reason", "")).casefold(), DebugStoppedReason.UNKNOWN)
                self._last_stop_reason = reason
                self._events.append(kind="stopped", reason=reason, thread_id=self._active_thread_id, description=_text_or_none(body.get("description"), 1024))
                if self._state not in {DebuggerState.TERMINATING, DebuggerState.TERMINATED, DebuggerState.FAILED}:
                    self._state = DebuggerState.PAUSED
            elif event == "continued":
                await self._invalidate_stopped_data_locked()
                self._cached_thread_count = None
                self._events.append(kind="continued")
                if self._state not in {DebuggerState.TERMINATING, DebuggerState.TERMINATED, DebuggerState.FAILED}:
                    self._state = DebuggerState.RUNNING
            elif event == "exited":
                if not self._exited_event_recorded:
                    exit_code = body.get("exitCode")
                    self._events.append(kind="exited", exit_code=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None)
                    self._exited_event_recorded = True
            elif event == "terminated":
                await self._invalidate_stopped_data_locked()
                self._record_terminal_event_locked()
                self._debuggee_termination_confirmed = True
                if self._state not in {DebuggerState.TERMINATING, DebuggerState.FAILED}:
                    self._state = DebuggerState.TERMINATED
            elif event == "output":
                text, truncated = _text(body.get("output"), "", 16_384)
                output = DebugOutputEvent(sequence=self._events.last_sequence + 1, category=_text(body.get("category"), "console", 64)[0], output=text, truncated=truncated)
                self._events.append(kind="output", output=output)
            elif event == "breakpoint":
                self._events.append(kind="breakpoint")

    async def _invalidate_stopped_data(self) -> None:
        async with self._state_lock:
            await self._invalidate_stopped_data_locked()

    async def _invalidate_stopped_data_locked(self) -> None:
        self._handles.clear_stop()
        self._active_thread_id = None
        self._cached_thread_count = None
        for task in tuple(self._read_dap_tasks):
            task.cancel()

    async def _watch_adapter(self, handle: ProcessHandle) -> None:
        try:
            await handle.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        async with self._state_lock:
            if not self._closing and self._state not in {DebuggerState.TERMINATED, DebuggerState.TERMINATING}:
                self._state = DebuggerState.FAILED
                self._failure = "The debug adapter exited unexpectedly."
                self._handles.clear_all()
                # ProcessHandle.wait closes the owned Windows Job / POSIX
                # process group before returning, so a required-ownership
                # adapter crash also forces its debuggee descendants down.
                self._debuggee_termination_confirmed = handle.required_ownership and handle.ownership_established
                self._record_terminal_event_locked("The debug adapter exited and its owned process tree was closed.")

    async def _drain_stderr(self, handle: ProcessHandle) -> None:
        try:
            while chunk := await handle.stderr.read(65_536):
                self._stderr_bytes = min(_MAX_STDERR_BYTES, self._stderr_bytes + len(chunk))
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    async def _close_unlocked(self) -> None:
        if self._state in {DebuggerState.UNAVAILABLE, DebuggerState.STOPPED, DebuggerState.TERMINATED} and self._handle is None:
            return
        self._closing = True
        await self._set_state(DebuggerState.TERMINATING)
        await self._invalidate_stopped_data()
        client, handle = self._client, self._handle
        if client is not None and client.state.value == "running":
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.request("disconnect", {"terminateDebuggee": True}), timeout=3.0)
        if client is not None:
            await client.aclose(expected_eof=True)
        if handle is not None:
            with contextlib.suppress(Exception):
                await handle.aclose()
        for task in (self._watch_task, self._stderr_task):
            if task is not None:
                task.cancel()
        tasks = tuple(task for task in (self._watch_task, self._stderr_task) if task is not None)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._client = None
        self._handle = None
        self._watch_task = None
        self._stderr_task = None
        self._handles.clear_all()
        self._debuggee_termination_confirmed = True
        async with self._state_lock:
            self._record_terminal_event_locked("The debug session was stopped.")
            self._state = DebuggerState.TERMINATED

    async def _mark_failed(self, error: Exception) -> None:
        async with self._state_lock:
            self._state = DebuggerState.FAILED
            self._failure = "The debug-adapter session failed."
            self._handles.clear_all()

    async def _set_state(self, state: DebuggerState) -> None:
        async with self._state_lock:
            self._state = state

    def _set_initialize_capabilities(self, initialize: Mapping[str, object]) -> None:
        """Validate only capability flags Phase 1 actually consumes/exposes."""
        enabled: list[str] = []
        for name in _INITIALIZE_CAPABILITIES:
            value = initialize.get(name)
            if value is not None and not isinstance(value, bool):
                raise DebuggerRequestError("The debug adapter returned invalid capability data.")
            if value is True:
                enabled.append(name)
        self._capabilities = tuple(enabled)
        self._supports_configuration_done = initialize.get("supportsConfigurationDoneRequest") is True

    def _record_terminal_event_locked(self, description: str | None = None) -> None:
        if self._terminal_event_recorded:
            return
        self._events.append(kind="terminated", description=description)
        self._terminal_event_recorded = True

    def _status_locked(self) -> DebugSessionStatus:
        return DebugSessionStatus(
            state=self._state,
            backend_id=self._backend.backend_id if self._state is not DebuggerState.UNAVAILABLE else None,
            session_generation=self._session_generation,
            stop_generation=self._stop_generation,
            capabilities=self._capabilities,
            active_thread_id=self._active_thread_id,
            last_event_sequence=self._events.last_sequence,
            dropped_event_count=self._events.dropped_count,
            debuggee_termination_confirmed=self._debuggee_termination_confirmed,
            failure=self._failure,
        )

    def _require_state(self, *states: DebuggerState) -> None:
        if self._state is DebuggerState.UNAVAILABLE:
            raise DebuggerUnavailableError(self._adapter_info.unavailable_reason or "No debugger adapter is available.")
        if self._state is DebuggerState.FAILED:
            raise DebuggerFailedError("The debug-adapter session failed; stop it before another launch.")
        if self._state not in states:
            raise DebuggerStateError("The requested debugger operation is not valid in the current state.")

    def _require_paused(self) -> int:
        self._require_state(DebuggerState.PAUSED)
        return self._stop_generation

    def _resolve(self, token: str, kind: str) -> int:
        return self._handles.resolve(token, kind, self._session_generation, self._stop_generation)

    def _check_stop_generation(self, generation: int) -> None:
        if self._state is not DebuggerState.PAUSED or self._stop_generation != generation:
            raise DebuggerStaleDataError("Paused debugger data changed before the request completed.")

    def _current_native_thread_id(self) -> int | None:
        if self._active_thread_id is None:
            return None
        with contextlib.suppress(DebuggerHandleExpiredError):
            return self._resolve(self._active_thread_id, "thread")
        return None

    def _normalize_source(self, value: object) -> DebugSource | None:
        if not isinstance(value, Mapping):
            return None
        path = value.get("path")
        name = _text_or_none(value.get("name"), 1024)
        if isinstance(path, str):
            try:
                return DebugSource(kind="workspace", path=self._workspace.validate_reported_path(path), name=name)
            except WorkspaceError:
                return DebugSource(kind="omitted", name=name)
        return DebugSource(kind="omitted", name=name) if name is not None else None

    def _normalize_variable(self, item: Mapping[str, object], generation: int) -> DebugVariable:
        value, truncated = _text(item.get("value"), "", 16_384)
        reference = item.get("variablesReference")
        variables_id = self._handles.put("variables", int(reference), self._session_generation, generation) if _positive_int(reference) else None
        return DebugVariable(
            name=_text(item.get("name"), "<variable>", 4096)[0],
            value=value,
            type=_text_or_none(item.get("type"), 4096),
            evaluate_name=_text_or_none(item.get("evaluateName"), 4096),
            variables_id=variables_id,
            named_variables=item.get("namedVariables") if _nonnegative_int(item.get("namedVariables")) else None,
            indexed_variables=item.get("indexedVariables") if _nonnegative_int(item.get("indexedVariables")) else None,
            truncated=truncated,
        )

    @staticmethod
    def _safe_exception(error: Exception) -> Exception:
        if isinstance(error, (DebuggerUnavailableError, DebuggerRequestError, DebuggerUnsupportedError, DebuggerStateError)):
            return error
        return DebuggerFailedError("The debug-adapter session could not be launched safely.")


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _dap_coordinate(value: object) -> int | None:
    return value - 1 if _positive_int(value) else None


def _text(value: object, fallback: str, maximum: int) -> tuple[str, bool]:
    if not isinstance(value, str):
        return fallback, False
    # JSON permits escaped lone surrogates, but writing one back through an MCP
    # stdio transport can fail.  C0/C1 controls (except normal text layout
    # whitespace) are likewise made inert before any output reaches a client.
    normalized = value.encode("utf-8", "replace").decode("utf-8")
    normalized = "".join(
        character if character in "\t\n\r" or (ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F) else "\uFFFD"
        for character in normalized
    )
    return normalized[:maximum], len(normalized) > maximum


def _text_or_none(value: object, maximum: int) -> str | None:
    return _text(value, "", maximum)[0] if isinstance(value, str) else None
