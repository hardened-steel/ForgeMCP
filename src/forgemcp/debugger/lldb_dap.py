"""Interpret LLDB-DAP messages as one bounded, adapter-independent debug session."""

# The concrete driver keeps explicit protocol operations and their lifecycle together.
# pylint: disable=too-many-lines

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from contextlib import AsyncExitStack, suppress
import json
from math import isfinite
from pathlib import Path
from typing import cast
from uuid import uuid4

import psutil
from pydantic import JsonValue, ValidationError

from forgemcp.toolchain.errors import ToolchainError
from forgemcp.toolchain.spec import ToolSpec
from forgemcp.toolchain.tools.lldb_dap import (
    Connection,
    DapEvent,
    DapRequest,
    DapResponse,
    Methods,
)
from forgemcp.workspace.path import WorkspacePath

from . import models as m
from .errors import (
    DebugAdapterError,
    DebugError,
    DebugReferenceExpiredError,
    DebugStaleReferenceError,
    DebugStateError,
    DebugTimeoutError,
    DebugUnsupportedError,
)
from .session import DebugSession


def object_value(value: JsonValue) -> dict[str, JsonValue]:
    """Require an adapter object without coercing malformed protocol data."""
    if not isinstance(value, dict):
        raise DebugAdapterError("The adapter returned a non-object result.")
    return value


def list_value(value: JsonValue) -> list[JsonValue]:
    """Require an adapter array without silently inventing empty results."""
    if not isinstance(value, list):
        raise DebugAdapterError("The adapter returned a non-array result.")
    return value


def is_integer(value: object) -> bool:
    """Recognize protocol integers without accepting JSON booleans."""
    return isinstance(value, int) and not isinstance(value, bool)


def integer(value: JsonValue) -> int:
    """Require an actual protocol integer, excluding booleans."""
    if not is_integer(value):
        raise DebugAdapterError("The adapter returned an invalid integer.")
    return value


def string(value: JsonValue) -> str:
    """Require text instead of interpreting arbitrary adapter values."""
    if not isinstance(value, str):
        raise DebugAdapterError("The adapter returned invalid text.")
    return value


class LldbDapSession(DebugSession):
    """Own DAP request correlation and target state while ToolSpec owns its process."""

    # One explicit adapter implements the agreed inspection/control interface and state machine.
    # pylint: disable=too-many-instance-attributes,too-many-public-methods

    MAX_EXECUTIONS = 64
    MAX_HANDLES = 10000
    MAX_RECORDS = 1000
    MAX_MEMORY = 65536

    def __init__(
        self,
        session_id: str,
        tool: ToolSpec,
        *,
        project_root: Path,
        storage_root: Path,
        timeouts: m.DebugTimeouts = m.DebugTimeouts(),
    ) -> None:
        """Bind a selected executable without starting the adapter or another service."""
        self.tool = tool
        self.project_root = project_root.resolve()
        self.storage_root = storage_root.resolve()
        self.timeouts = timeouts
        self.state = m.SessionSnapshot(session_id=session_id)
        self.connection: Connection | None = None
        self.lifetime = AsyncExitStack()
        self.reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[dict[str, JsonValue]]] = {}
        self.executions: OrderedDict[str, asyncio.Future[m.WaitResult]] = OrderedDict()
        self.control_lock = asyncio.Lock()
        self.initialized = asyncio.Event()
        self.next_sequence = 0
        self.generation = 0
        self.handles: dict[str, tuple[str, int | str]] = {}
        self.handle_ids: dict[tuple[str, int | str], str] = {}
        self.thread_ids: dict[str, int] = {}
        self.last_thread_id: int | None = None
        self.close_task: asyncio.Task[m.CloseResult] | None = None
        self.configuration_done = False

    def snapshot(self) -> m.SessionSnapshot:
        """Return an independent immutable observation of retained state."""
        return self.state.model_copy(deep=True)

    def publish(self, **changes: object) -> None:
        """Advance state atomically within the event loop and complete execution observers."""
        self.state = self.state.model_copy(
            update={**changes, "revision": self.state.revision + 1},
        )
        outcome = None
        if self.state.phase == m.SessionPhase.FAILED:
            outcome = "failed"
        elif self.state.target_state == m.TargetState.EXITED:
            outcome = "exited"
        elif self.state.target_state in (
            m.TargetState.STOPPED,
            m.TargetState.PARTIALLY_STOPPED,
        ):
            outcome = "stopped"
        elif self.state.phase == m.SessionPhase.CLOSED:
            outcome = "closed"
        if outcome is not None:
            result = m.WaitResult(outcome=outcome, snapshot=self.snapshot())
            for future in self.executions.values():
                if not future.done():
                    future.set_result(result)

    def invalidate(self) -> None:
        """Discard suspension-scoped handles before dispatching a resume operation."""
        self.generation += 1
        self.handles.clear()
        self.handle_ids.clear()
        self.publish(
            inspection_generation=None,
            status=None,
            target_state=m.TargetState.UNKNOWN,
        )

    def deadline(self, timeout: float | None, default: float) -> float:
        """Validate finite positive deadlines for direct business callers."""
        value = default if timeout is None else timeout
        if not isfinite(value) or value <= 0:
            raise DebugStateError("Debugger deadlines must be finite and greater than zero.")
        return value

    def managed(self, reference: WorkspacePath, *, directory: bool = False) -> Path:
        """Resolve an existing input inside its own managed root without reading external files."""
        if reference.area == "root":
            raise DebugStateError("Debugger input files must be inside project/ or storage/.")
        if any(ord(character) < 32 or character == "`" for character in reference):
            raise DebugStateError("Debugger input paths cannot contain control characters.")
        root = self.project_root if reference.area == "project" else self.storage_root
        try:
            path = (root / reference.relative).resolve()
            if not path.is_relative_to(root):
                raise DebugStateError("Debugger input path escaped its managed root.")
            if not (path.is_dir() if directory else path.is_file()):
                raise DebugStateError(f"Debugger input does not exist: {reference}.")
            return path
        except OSError as error:
            raise DebugStateError(f"Cannot inspect debugger input: {reference}.") from error

    def source_path(self, value: str) -> WorkspacePath | None:
        """Qualify adapter locations without reading source files or following source contents."""
        try:
            native = Path(value)
            if not native.is_absolute():
                native = self.project_root / native
            native = native.resolve()
            for prefix, root in (
                ("project", self.project_root),
                ("storage", self.storage_root),
            ):
                if native.is_relative_to(root):
                    suffix = native.relative_to(root).as_posix()
                    return WorkspacePath(
                        f"{prefix}/{suffix}" if suffix != "." else f"{prefix}/"
                    )
            return WorkspacePath("root/" + native.as_posix())
        except (OSError, ValueError):
            return None

    def source_arguments(self, options: m.SourceOptions) -> dict[str, JsonValue]:
        """Translate explicit source mappings and managed symbol paths into LLDB options."""
        arguments: dict[str, JsonValue] = {}
        if options.source_mappings:
            mappings: list[JsonValue] = []
            for mapping in options.source_mappings:
                if any(character in mapping.source for character in ("\0", "\n", "\r")):
                    raise DebugStateError(
                        "Source mapping prefixes cannot contain control characters."
                    )
                mappings.append(
                    [mapping.source, str(self.managed(mapping.destination, directory=True))]
                )
            arguments["sourceMap"] = mappings
        if options.symbol_directories:
            directories = [
                str(self.managed(item, directory=True)) for item in options.symbol_directories
            ]
            if any(
                ord(character) < 32 or character == "`"
                for directory in directories
                for character in directory
            ):
                raise DebugStateError(
                    "Symbol search paths cannot contain control characters or backticks.",
                )
            arguments["initCommands"] = [
                "settings set target.debug-file-search-paths "
                + " ".join(json.dumps(item) for item in directories),
            ]
        return arguments

    def active(self, *, live: bool = False) -> None:
        """Require an operational connection and optionally an executable target."""
        if self.state.phase != m.SessionPhase.ACTIVE:
            raise DebugStateError("The debugger session is not active.")
        if live and self.state.mode == "dump":
            raise DebugUnsupportedError("Crash dumps support inspection only.")

    def check_stop(self, token: m.StopToken) -> None:
        """Reject foreign or invalidated stop references before and after inspection."""
        self.active()
        if (
            token.session_id != self.state.session_id
            or token.generation != self.state.inspection_generation
            or self.state.target_state
            not in (
                m.TargetState.STOPPED,
                m.TargetState.PARTIALLY_STOPPED,
                m.TargetState.DUMP,
            )
        ):
            raise DebugStaleReferenceError("The inspection reference is no longer current.")

    def thread(self, reference: m.ThreadRef) -> int:
        """Resolve an opaque thread handle belonging to this session."""
        if (
            reference.session_id != self.state.session_id
            or reference.handle not in self.thread_ids
        ):
            raise DebugStaleReferenceError(
                "The thread reference is not retained by this session."
            )
        return self.thread_ids[reference.handle]

    def thread_reference(self, identifier: int) -> m.ThreadRef:
        """Reuse an opaque thread identity across multiple suspension generations."""
        for handle, native in self.thread_ids.items():
            if native == identifier:
                return m.ThreadRef(session_id=self.state.session_id, handle=handle)
        if len(self.thread_ids) >= self.MAX_HANDLES:
            raise DebugStateError("The session thread-handle limit was reached.")
        handle = uuid4().hex
        self.thread_ids[handle] = identifier
        return m.ThreadRef(session_id=self.state.session_id, handle=handle)

    def reference(
        self, kind: str, native: int | str
    ) -> m.FrameRef | m.VariablesRef | m.MemoryRef:
        """Intern one suspension-scoped adapter identity without exposing its raw value."""
        key = (kind, native)
        handle = self.handle_ids.get(key)
        if handle is None:
            if len(self.handles) >= self.MAX_HANDLES:
                raise DebugStateError(
                    "The inspection-handle limit was reached; resume to release old handles."
                )
            handle = uuid4().hex
            self.handles[handle] = key
            self.handle_ids[key] = handle
        classes = {"frame": m.FrameRef, "variables": m.VariablesRef, "memory": m.MemoryRef}
        return classes[kind](
            session_id=self.state.session_id,
            generation=self.generation,
            handle=handle,
        )

    def resolve_reference(
        self, reference: m.FrameRef | m.VariablesRef | m.MemoryRef, kind: str
    ) -> int | str:
        """Validate a generation and reference kind before reading the adapter object."""
        self.check_stop(reference)
        found = self.handles.get(reference.handle)
        if found is None or found[0] != kind:
            raise DebugStaleReferenceError(
                "The inspection handle has an invalid type or has expired."
            )
        return found[1]

    def execution(self) -> m.ExecutionRef:
        """Retain a bounded future before a command can produce its stop event."""
        while len(self.executions) >= self.MAX_EXECUTIONS:
            oldest, future = next(iter(self.executions.items()))
            if not future.done():
                raise DebugStateError("Too many executions are awaiting a stop.")
            del self.executions[oldest]
        identifier = uuid4().hex
        self.executions[identifier] = asyncio.get_running_loop().create_future()
        return m.ExecutionRef(session_id=self.state.session_id, execution_id=identifier)

    async def request(
        self,
        command: str,
        arguments: dict[str, JsonValue],
        *,
        timeout: float | None = None,
        mutates: bool = False,
    ) -> dict[str, JsonValue]:
        """Correlate one bounded DAP response while the independent reader handles events."""
        limit = self.deadline(timeout, self.timeouts.request)
        if self.connection is None:
            raise DebugStateError("The debugger transport is not connected.")
        if self.state.phase == m.SessionPhase.FAILED:
            raise DebugAdapterError("The debugger adapter has failed.")
        self.next_sequence += 1
        sequence = self.next_sequence
        future: asyncio.Future[dict[str, JsonValue]] = (
            asyncio.get_running_loop().create_future()
        )
        self.pending[sequence] = future
        try:
            async with asyncio.timeout(limit):
                await self.connection.send(DapRequest(sequence, command, arguments))
                return await asyncio.shield(future)
        except TimeoutError as error:
            if mutates and self.state.target_state in (
                m.TargetState.RUNNING,
                m.TargetState.UNKNOWN,
            ):
                self.publish(target_state=m.TargetState.UNKNOWN, status=None)
            raise DebugTimeoutError(
                f"Debugger request {command} exceeded its deadline.",
                outcome_unknown=mutates,
            ) from error
        except (ToolchainError, OSError) as error:
            raise DebugAdapterError("Cannot send a request to the debugger adapter.") from error
        finally:
            self.pending.pop(sequence, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # Cancellation can race a failure delivered to the shielded future.
                future.exception()

    def fail(self, message: str) -> None:
        """Fail the connection without claiming that the target exited."""
        self.publish(
            phase=m.SessionPhase.FAILED,
            target_state=m.TargetState.UNKNOWN,
            inspection_generation=None,
            status=m.FailedStatus(message=message),
        )
        for future in self.pending.values():
            if not future.done():
                future.set_exception(DebugAdapterError(message))
        self.initialized.set()

    async def receive(self) -> None:
        """Continuously dispatch correlated responses, events, and unsupported reverse requests."""
        # Keep all three wire envelope kinds visible at their sole dispatch site.
        # pylint: disable=too-many-branches
        try:
            if self.connection is None:
                return
            async for message in self.connection.messages():
                if isinstance(message, DapResponse):
                    future = self.pending.get(message.request_seq)
                    if future is not None and not future.done():
                        if message.success:
                            future.set_result(
                                object_value(message.body) if message.body is not None else {}
                            )
                        else:
                            future.set_exception(
                                DebugAdapterError(message.message or "Debugger request failed.")
                            )
                elif isinstance(message, DapEvent):
                    self.event(message)
                elif isinstance(message, DapRequest):
                    self.next_sequence += 1
                    await self.connection.send(
                        DapResponse(
                            self.next_sequence,
                            message.seq,
                            message.command,
                            False,
                            message="Reverse debugger requests are not supported.",
                        )
                    )
            if self.state.phase not in (m.SessionPhase.CLOSING, m.SessionPhase.CLOSED):
                self.fail("The debugger adapter connection closed unexpectedly.")
        except (ToolchainError, DebugError, OSError, ValueError, ValidationError):
            if self.state.phase not in (m.SessionPhase.CLOSING, m.SessionPhase.CLOSED):
                self.fail("The debugger adapter connection failed.")
        finally:
            if self.state.phase == m.SessionPhase.FAILED:
                # No consumer remains after a framing failure; stop draining into an
                # unconsumed process queue, preserving the failed domain observation.
                with suppress(ToolchainError, OSError):
                    await self.lifetime.aclose()
                self.connection = None

    def event(self, message: DapEvent) -> None:
        """Interpret execution events without retaining another full protocol journal."""
        body = object_value(message.body) if message.body is not None else {}
        if message.event == "initialized":
            self.initialized.set()
        elif message.event == "process":
            pid = body.get("systemProcessId")
            if is_integer(pid):
                self.publish(target_pid=pid)
        elif message.event == "stopped":
            self.generation += 1
            self.handles.clear()
            self.handle_ids.clear()
            thread_id = body.get("threadId")
            reference = None
            if is_integer(thread_id):
                self.last_thread_id = thread_id
                reference = self.thread_reference(thread_id).handle
            all_stopped = body.get("allThreadsStopped") is True
            self.publish(
                phase=m.SessionPhase.ACTIVE,
                target_state=(
                    m.TargetState.DUMP
                    if self.state.mode == "dump"
                    else (
                        m.TargetState.STOPPED
                        if all_stopped
                        else m.TargetState.PARTIALLY_STOPPED
                    )
                ),
                inspection_generation=self.generation,
                status=m.StoppedStatus(
                    reason=string(body.get("reason", "unknown")),
                    thread_id=reference,
                    description=cast(str | None, body.get("description", body.get("text"))),
                    all_threads_stopped=all_stopped,
                ),
            )
        elif message.event == "continued":
            self.invalidate()
            self.publish(target_state=m.TargetState.RUNNING)
        elif message.event == "exited":
            self.invalidate()
            self.publish(
                target_state=m.TargetState.EXITED,
                status=m.ExitedStatus(exit_code=integer(body.get("exitCode"))),
            )
        elif message.event == "terminated":
            self.publish(phase=m.SessionPhase.CLOSED)
        elif message.event == "thread" and body.get("reason") == "exited":
            identifier = body.get("threadId")
            self.thread_ids = {
                handle: native
                for handle, native in self.thread_ids.items()
                if native != identifier
            }
        elif message.event == "invalidated":
            self.generation += 1
            self.handles.clear()
            self.handle_ids.clear()
            self.publish(inspection_generation=self.generation)

    async def connect(self, mode: str) -> None:
        """Start the selected ToolSpec connection and negotiate supported domain operations."""
        if self.state.phase != m.SessionPhase.CREATED:
            raise DebugStateError("A debugger session can open only one target.")
        self.publish(mode=mode, phase=m.SessionPhase.OPENING)
        methods = cast(Methods, self.tool.methods)
        if "connect" not in methods:
            raise DebugUnsupportedError("The selected tool has no debugger transport.")
        self.connection = await self.lifetime.enter_async_context(
            methods["connect"](self.project_root)
        )
        self.publish(adapter_process_id=self.connection.process_id)
        self.reader = asyncio.create_task(self.receive())
        capabilities = await self.request(
            "initialize",
            {
                "clientID": "forgemcp",
                "adapterID": "lldb",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsVariablePaging": True,
                "supportsRunInTerminalRequest": False,
                "supportsMemoryReferences": True,
                "sourceInitFile": False,
            },
        )
        filters = []
        for value in list_value(capabilities.get("exceptionBreakpointFilters", [])):
            item = object_value(value)
            filters.append(
                m.ExceptionFilter(
                    id=string(item.get("filter")),
                    label=string(item.get("label")),
                    default=item.get("default") is True,
                )
            )
        self.configuration_done = capabilities.get("supportsConfigurationDoneRequest") is True
        self.publish(
            capabilities=m.DebugCapabilities(
                function_breakpoints=capabilities.get("supportsFunctionBreakpoints") is True,
                exception_info=capabilities.get("supportsExceptionInfoRequest") is True,
                modules=capabilities.get("supportsModulesRequest") is True,
                read_memory=capabilities.get("supportsReadMemoryRequest") is True,
                disassemble=capabilities.get("supportsDisassembleRequest") is True,
                # supportsVariablePaging belongs to initialize's client arguments.
                # LLDB root scopes have inconsistent start-offset support, so this
                # driver uses local windows within the bounded protocol response.
                variable_paging=False,
                exception_filters=filters,
            ),
        )

    async def start(
        self,
        mode: str,
        arguments: dict[str, JsonValue],
        initial: m.BreakpointPlan,
        timeout: float | None,
    ) -> m.ExecutionReceipt:
        """Configure while launch is pending, preserving early stopped events for later waits."""
        # DAP startup is an ordered operation, with cleanup beside its failure paths.
        # pylint: disable=too-many-branches,too-many-statements
        limit = self.deadline(timeout, self.timeouts.startup)
        command: asyncio.Task[dict[str, JsonValue]] | None = None
        async with self.control_lock:
            try:
                async with asyncio.timeout(limit):
                    await self.connect(mode)
                    execution = self.execution()
                    command = asyncio.create_task(
                        self.request(
                            "launch" if mode == "launch" else "attach",
                            arguments,
                            timeout=limit,
                            mutates=True,
                        )
                    )
                    ready = asyncio.create_task(self.initialized.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (command, ready),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if command in done:
                            await command
                        if not self.initialized.is_set():
                            await ready
                    finally:
                        ready.cancel()
                        with suppress(asyncio.CancelledError):
                            await ready
                    if self.state.phase == m.SessionPhase.FAILED:
                        raise DebugAdapterError("The debugger failed during initialization.")
                    if mode != "dump":
                        for source in initial.sources:
                            await self.set_breakpoints(source.source, source.points)
                        if initial.functions:
                            await self.set_function_breakpoints(initial.functions)
                        filters = initial.exception_filters
                        if filters is None:
                            filters = [
                                item.id
                                for item in self.state.capabilities.exception_filters
                                if item.default
                            ]
                        if filters or self.state.capabilities.exception_filters:
                            await self.set_exception_breakpoints(filters)
                    if self.configuration_done:
                        await self.request("configurationDone", {})
                    await command
                    if mode == "dump":
                        if self.state.inspection_generation is None:
                            self.generation += 1
                        self.publish(
                            phase=m.SessionPhase.ACTIVE,
                            target_state=m.TargetState.DUMP,
                            inspection_generation=self.generation,
                        )
                    elif self.state.phase == m.SessionPhase.OPENING:
                        self.publish(
                            phase=m.SessionPhase.ACTIVE,
                            target_state=m.TargetState.RUNNING,
                        )
                    return m.ExecutionReceipt(execution=execution, snapshot=self.snapshot())
            except TimeoutError as error:
                await self.abort_start("Debugger startup exceeded its deadline.")
                raise DebugTimeoutError(
                    "Debugger startup exceeded its deadline.",
                    outcome_unknown=True,
                ) from error
            except BaseException:
                await self.abort_start("Debugger startup did not complete.")
                raise
            finally:
                if command is not None:
                    if not command.done():
                        command.cancel()
                    with suppress(asyncio.CancelledError, DebugError):
                        await command

    async def abort_start(self, message: str) -> None:
        """Release a failed or cancelled opening while retaining its failure observation."""
        await self.close()
        self.fail(message)

    async def launch(
        self,
        options: m.LaunchOptions,
        initial: m.BreakpointPlan,
        *,
        timeout: float | None = None,
    ) -> m.ExecutionReceipt:
        """Launch a managed native executable with initial breakpoints and literal argv."""
        executable = self.managed(options.executable)
        if executable.suffix.lower() in (".bat", ".cmd"):
            raise DebugStateError(
                "Debugger launch requires a native executable, not a batch file."
            )
        arguments: dict[str, JsonValue] = {
            **self.source_arguments(options),
            "program": str(executable),
            "args": list(options.arguments),
            "cwd": str(self.managed(options.cwd, directory=True)),
            "env": dict(options.environment),
            "stopOnEntry": options.stop_on_entry,
            "runInTerminal": False,
        }
        return await self.start("launch", arguments, initial, timeout)

    async def attach(
        self,
        options: m.AttachOptions,
        initial: m.BreakpointPlan,
        *,
        timeout: float | None = None,
    ) -> m.ExecutionReceipt:
        """Attach to the supplied PID without adopting ownership of the external program."""
        if options.expected_started_at is not None:
            try:
                if psutil.Process(options.pid).create_time() != options.expected_started_at:
                    raise DebugStateError(
                        "The selected PID belongs to a different process instance."
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
                raise DebugStateError("Cannot verify the selected process identity.") from error
        arguments: dict[str, JsonValue] = {
            **self.source_arguments(options),
            "pid": options.pid,
            "stopOnEntry": options.stop_on_entry,
        }
        if options.executable is not None:
            arguments["program"] = str(self.managed(options.executable))
        return await self.start("attach", arguments, initial, timeout)

    async def open_dump(
        self,
        options: m.DumpOptions,
        *,
        timeout: float | None = None,
    ) -> m.SessionSnapshot:
        """Open an existing managed dump through LLDB's core-file attach path."""
        arguments: dict[str, JsonValue] = {
            **self.source_arguments(options),
            "coreFile": str(self.managed(options.dump_file)),
        }
        if options.executable is not None:
            arguments["program"] = str(self.managed(options.executable))
        await self.start("dump", arguments, m.BreakpointPlan(), timeout)
        return self.snapshot()

    def configurable(self) -> None:
        """Allow initial configuration while opening and later live changes while active."""
        if self.state.phase not in (m.SessionPhase.OPENING, m.SessionPhase.ACTIVE):
            raise DebugStateError(
                "The debugger cannot configure breakpoints in its current state."
            )
        if self.state.mode == "dump":
            raise DebugUnsupportedError("Crash dumps do not support breakpoints.")

    def breakpoints(
        self,
        body: dict[str, JsonValue],
        points: list[m.SourceBreakpoint],
    ) -> list[m.Breakpoint]:
        """Preserve requested locations beside the adapter's actual verification results."""
        values = list_value(body.get("breakpoints", []))
        if len(values) > self.MAX_RECORDS:
            raise DebugAdapterError("The adapter returned too many breakpoint records.")
        result = []
        for index, value in enumerate(values):
            item = object_value(value)
            result.append(
                m.Breakpoint(
                    requested_line=points[index].line if index < len(points) else None,
                    line=integer(item["line"]) if "line" in item else None,
                    verified=item.get("verified") is True,
                    message=cast(str | None, item.get("message")),
                )
            )
        return result

    async def set_breakpoints(
        self,
        source: WorkspacePath,
        points: list[m.SourceBreakpoint],
        *,
        timeout: float | None = None,
    ) -> m.BreakpointsResult:
        """Replace one file's breakpoints, including an empty request that clears them."""
        self.configurable()
        if len(points) > self.MAX_RECORDS:
            raise DebugStateError("Select at most 1000 breakpoints per file.")
        path = self.managed(source)
        body = await self.request(
            "setBreakpoints",
            {
                "source": {"path": str(path)},
                "breakpoints": [{"line": point.line} for point in points],
                "sourceModified": False,
            },
            timeout=timeout,
        )
        return m.BreakpointsResult(
            snapshot=self.snapshot(),
            source=source,
            breakpoints=self.breakpoints(body, points),
        )

    async def set_function_breakpoints(
        self,
        functions: list[str],
        *,
        timeout: float | None = None,
    ) -> m.BreakpointsResult:
        """Replace function breakpoints using names interpreted only as adapter data."""
        self.configurable()
        if not self.state.capabilities.function_breakpoints:
            raise DebugUnsupportedError("The adapter does not support function breakpoints.")
        if len(functions) > self.MAX_RECORDS or any(
            not value or len(value) > 4096 or "\0" in value for value in functions
        ):
            raise DebugStateError(
                "Function breakpoints require bounded, nonempty literal names."
            )
        body = await self.request(
            "setFunctionBreakpoints",
            {"breakpoints": [{"name": name} for name in functions]},
            timeout=timeout,
        )
        return m.BreakpointsResult(
            snapshot=self.snapshot(),
            breakpoints=self.breakpoints(body, []),
        )

    async def set_exception_breakpoints(
        self,
        filters: list[str],
        *,
        timeout: float | None = None,
    ) -> m.ExceptionBreakpointsResult:
        """Apply only exception filters explicitly advertised by the adapter."""
        self.configurable()
        available = {item.id for item in self.state.capabilities.exception_filters}
        if len(filters) > 100 or any(item not in available for item in filters):
            raise DebugStateError("Select only advertised exception breakpoint filters.")
        await self.request(
            "setExceptionBreakpoints",
            {"filters": list(filters)},
            timeout=timeout,
        )
        return m.ExceptionBreakpointsResult(snapshot=self.snapshot(), filters=list(filters))

    async def execute(
        self,
        command: str,
        arguments: dict[str, JsonValue],
        timeout: float | None,
        *,
        invalidate: bool,
    ) -> m.ExecutionReceipt:
        """Record an execution before sending so an immediate stop cannot be lost."""
        execution = self.execution()
        if invalidate:
            self.invalidate()
        await self.request(command, arguments, timeout=timeout, mutates=True)
        if invalidate and self.state.target_state == m.TargetState.UNKNOWN:
            self.publish(target_state=m.TargetState.RUNNING)
        return m.ExecutionReceipt(execution=execution, snapshot=self.snapshot())

    async def selected_thread(self) -> int:
        """Find a retained thread or query one when pause needs an initial identity."""
        if self.last_thread_id is not None:
            return self.last_thread_id
        result = await self.threads()
        if not result.threads:
            raise DebugStateError("The debugger has no target thread for this operation.")
        return self.thread(result.threads[0].reference)

    async def resume(
        self,
        stop: m.StopToken,
        *,
        timeout: float | None = None,
    ) -> m.ExecutionReceipt:
        """Continue the selected stop while invalidating all inspection references."""
        async with self.control_lock:
            self.active(live=True)
            self.check_stop(stop)
            return await self.execute(
                "continue",
                {"threadId": await self.selected_thread()},
                timeout,
                invalidate=True,
            )

    async def step(
        self,
        thread: m.ThreadRef,
        stop: m.StopToken,
        kind: m.StepKind,
        *,
        timeout: float | None = None,
    ) -> m.ExecutionReceipt:
        """Perform a source-level step at the explicitly selected current thread."""
        async with self.control_lock:
            self.active(live=True)
            self.check_stop(stop)
            commands = {
                m.StepKind.OVER: "next",
                m.StepKind.INTO: "stepIn",
                m.StepKind.OUT: "stepOut",
            }
            return await self.execute(
                commands[kind],
                {"threadId": self.thread(thread), "singleThread": False},
                timeout,
                invalidate=True,
            )

    async def pause(self, *, timeout: float | None = None) -> m.ExecutionReceipt:
        """Request a stop without holding the control lock while waiting for that event."""
        async with self.control_lock:
            self.active(live=True)
            if self.state.target_state not in (m.TargetState.RUNNING, m.TargetState.UNKNOWN):
                raise DebugStateError("The target is not running.")
            return await self.execute(
                "pause",
                {"threadId": await self.selected_thread()},
                timeout,
                invalidate=False,
            )

    async def wait_for_stop(
        self,
        execution: m.ExecutionRef,
        *,
        timeout: float,
    ) -> m.WaitResult:
        """Wait on a retained command outcome without cancelling its future or the target."""
        limit = self.deadline(timeout, self.timeouts.request)
        if execution.session_id != self.state.session_id:
            raise DebugStaleReferenceError("The execution belongs to a different session.")
        future = self.executions.get(execution.execution_id)
        if future is None:
            raise DebugReferenceExpiredError("The execution outcome has expired or is unknown.")
        try:
            async with asyncio.timeout(limit):
                result = await asyncio.shield(future)
                return result.model_copy(deep=True)
        except TimeoutError:
            return m.WaitResult(outcome="timeout", snapshot=self.snapshot())

    async def threads(self, *, timeout: float | None = None) -> m.ThreadsResult:
        """Read named threads without leaking adapter identifiers into public references."""
        self.active()
        body = await self.request("threads", {}, timeout=timeout)
        values = list_value(body.get("threads", []))
        threads = []
        for value in values[: self.MAX_RECORDS]:
            item = object_value(value)
            threads.append(
                m.Thread(
                    reference=self.thread_reference(integer(item.get("id"))),
                    name=string(item.get("name")),
                )
            )
        return m.ThreadsResult(
            snapshot=self.snapshot(),
            threads=threads,
            truncated=len(values) > self.MAX_RECORDS,
        )

    def location(self, value: dict[str, JsonValue]) -> m.SourceLocation:
        """Return source lines and addresses without mislabelling UTF-16 columns."""
        source = value.get("source", value.get("location"))
        path = None
        if isinstance(source, dict) and isinstance(source.get("path"), str):
            path = self.source_path(source["path"])
        line = value.get("line")
        address = value.get("instructionPointerReference")
        return m.SourceLocation(
            path=path,
            line=line if is_integer(line) and line > 0 else None,
            address=address if isinstance(address, str) else None,
        )

    async def stack(
        self,
        thread: m.ThreadRef,
        stop: m.StopToken,
        page: m.PageRequest,
        *,
        timeout: float | None = None,
    ) -> m.StackResult:
        """Read a stack page and reject it if execution changes before its response."""
        self.check_stop(stop)
        identifier = self.thread(thread)
        if (
            self.state.target_state == m.TargetState.PARTIALLY_STOPPED
            and isinstance(self.state.status, m.StoppedStatus)
            and self.state.status.thread_id != thread.handle
        ):
            raise DebugStateError("Only the confirmed stopped thread can be inspected.")
        body = await self.request(
            "stackTrace",
            {"threadId": identifier, "startFrame": page.start, "levels": page.count},
            timeout=timeout,
        )
        self.check_stop(stop)
        values = list_value(body.get("stackFrames", []))
        frames = []
        for value in values[: page.count]:
            item = object_value(value)
            frames.append(
                m.StackFrame(
                    reference=cast(
                        m.FrameRef,
                        self.reference("frame", integer(item.get("id"))),
                    ),
                    name=string(item.get("name")),
                    location=self.location(item),
                    memory=(
                        cast(
                            m.MemoryRef,
                            self.reference("memory", item["instructionPointerReference"]),
                        )
                        if isinstance(item.get("instructionPointerReference"), str)
                        else None
                    ),
                )
            )
        total_value = body.get("totalFrames")
        total = total_value if is_integer(total_value) and total_value >= 0 else None
        next_start = (
            page.start + len(frames)
            if frames
            and (
                (total is not None and page.start + len(frames) < total)
                or (total is None and len(frames) == page.count)
            )
            else None
        )
        return m.StackResult(
            snapshot=self.snapshot(),
            frames=frames,
            total=total,
            next_start=next_start,
        )

    def container(self, item: dict[str, JsonValue]) -> m.VariablesRef | None:
        """Create a child handle only for an actual nonzero variables reference."""
        native = item.get("variablesReference", 0)
        if not is_integer(native) or native < 0:
            raise DebugAdapterError("The adapter returned an invalid variables reference.")
        return cast(m.VariablesRef, self.reference("variables", native)) if native else None

    async def scopes(
        self,
        frame: m.FrameRef,
        *,
        timeout: float | None = None,
    ) -> m.ScopesResult:
        """Read frame scopes only while the frame's inspection generation remains current."""
        native = self.resolve_reference(frame, "frame")
        body = await self.request("scopes", {"frameId": native}, timeout=timeout)
        self.check_stop(frame)
        values = list_value(body.get("scopes", []))
        scopes = []
        for value in values[: self.MAX_RECORDS]:
            item = object_value(value)
            scopes.append(
                m.Scope(
                    name=string(item.get("name")),
                    reference=self.container(item),
                    expensive=item.get("expensive") is True,
                    named_variables=cast(int | None, item.get("namedVariables")),
                    indexed_variables=cast(int | None, item.get("indexedVariables")),
                )
            )
        return m.ScopesResult(
            snapshot=self.snapshot(),
            scopes=scopes,
            truncated=len(values) > self.MAX_RECORDS,
        )

    async def variables(
        self,
        reference: m.VariablesRef,
        page: m.PageRequest,
        *,
        timeout: float | None = None,
    ) -> m.VariablesResult:
        """Read rendered variable data without providing expression evaluation or mutation."""
        native = self.resolve_reference(reference, "variables")
        arguments: dict[str, JsonValue] = {"variablesReference": native}
        if self.state.capabilities.variable_paging:
            arguments.update(start=page.start, count=page.count)
        body = await self.request("variables", arguments, timeout=timeout)
        self.check_stop(reference)
        values = list_value(body.get("variables", []))
        selected = (
            values[: page.count]
            if self.state.capabilities.variable_paging
            else values[page.start : page.start + page.count]
        )
        variables = []
        for value in selected:
            item = object_value(value)
            address = item.get("memoryReference")
            variables.append(
                m.Variable(
                    name=string(item.get("name")),
                    value=string(item.get("value")),
                    type=cast(str | None, item.get("type")),
                    reference=self.container(item),
                    memory=(
                        cast(m.MemoryRef, self.reference("memory", address))
                        if isinstance(address, str)
                        else None
                    ),
                    named_variables=cast(int | None, item.get("namedVariables")),
                    indexed_variables=cast(int | None, item.get("indexedVariables")),
                )
            )
        more = (
            len(variables) == page.count
            if self.state.capabilities.variable_paging
            else page.start + len(selected) < len(values)
        )
        return m.VariablesResult(
            snapshot=self.snapshot(),
            variables=variables,
            next_start=page.start + len(variables) if more else None,
        )

    async def modules(
        self,
        page: m.PageRequest,
        *,
        timeout: float | None = None,
    ) -> m.ModulesResult:
        """Read a bounded module window when the adapter advertises that operation."""
        self.active()
        if not self.state.capabilities.modules:
            raise DebugUnsupportedError("The adapter does not support module inspection.")
        body = await self.request(
            "modules",
            {"startModule": page.start, "moduleCount": page.count},
            timeout=timeout,
        )
        values = list_value(body.get("modules", []))
        modules = []
        for value in values[: page.count]:
            item = object_value(value)
            path = item.get("path")
            modules.append(
                m.Module(
                    name=string(item.get("name")),
                    path=self.source_path(path) if isinstance(path, str) else None,
                    symbol_status=cast(str | None, item.get("symbolStatus")),
                    address_range=cast(str | None, item.get("addressRange")),
                )
            )
        total_value = body.get("totalModules")
        total = total_value if is_integer(total_value) and total_value >= 0 else None
        more = (
            page.start + len(modules) < total
            if total is not None
            else len(modules) == page.count
        )
        return m.ModulesResult(
            snapshot=self.snapshot(),
            modules=modules,
            total=total,
            next_start=page.start + len(modules) if modules and more else None,
        )

    async def exception_info(
        self,
        thread: m.ThreadRef,
        stop: m.StopToken,
        *,
        timeout: float | None = None,
    ) -> m.ExceptionInfo:
        """Read adapter-reported exception detail for the selected current stop."""
        self.check_stop(stop)
        if not self.state.capabilities.exception_info:
            raise DebugUnsupportedError("The adapter does not support exception detail.")
        body = await self.request(
            "exceptionInfo",
            {"threadId": self.thread(thread)},
            timeout=timeout,
        )
        self.check_stop(stop)
        details = body.get("details")
        return m.ExceptionInfo(
            snapshot=self.snapshot(),
            exception_id=string(body.get("exceptionId")),
            description=cast(str | None, body.get("description")),
            break_mode=string(body.get("breakMode")),
            details=json.dumps(details, ensure_ascii=False) if details is not None else None,
        )

    async def read_memory(
        self,
        reference: m.MemoryRef,
        offset: int,
        count: int,
        *,
        timeout: float | None = None,
    ) -> m.MemoryResult:
        """Read bounded bytes and explicitly preserve unavailable memory in sparse dumps."""
        native = self.resolve_reference(reference, "memory")
        if not self.state.capabilities.read_memory:
            raise DebugUnsupportedError("The adapter does not support reading memory.")
        if (
            not is_integer(count)
            or count < 1
            or count > self.MAX_MEMORY
            or not is_integer(offset)
        ):
            raise DebugStateError("Read between 1 and 65536 bytes with an integer offset.")
        body = await self.request(
            "readMemory",
            {"memoryReference": native, "offset": offset, "count": count},
            timeout=timeout,
        )
        self.check_stop(reference)
        data = body.get("data")
        if data is not None:
            try:
                if len(base64.b64decode(string(data), validate=True)) > count:
                    raise DebugAdapterError("The adapter returned excess memory data.")
            except ValueError as error:
                raise DebugAdapterError(
                    "The adapter returned malformed base64 memory data."
                ) from error
        unreadable = integer(body.get("unreadableBytes", 0))
        if unreadable < 0 or unreadable > count:
            raise DebugAdapterError("The adapter returned an invalid unreadable memory count.")
        return m.MemoryResult(
            snapshot=self.snapshot(),
            address=cast(str | None, body.get("address")),
            data=cast(str | None, data),
            unreadable_bytes=unreadable,
        )

    async def disassemble(
        self,
        reference: m.MemoryRef,
        offset: int,
        count: int,
        *,
        timeout: float | None = None,
    ) -> m.DisassemblyResult:
        """Read a bounded instruction window with exact textual addresses."""
        native = self.resolve_reference(reference, "memory")
        if not self.state.capabilities.disassemble:
            raise DebugUnsupportedError("The adapter does not support disassembly.")
        if (
            not is_integer(count)
            or count < 1
            or count > self.MAX_RECORDS
            or not is_integer(offset)
        ):
            raise DebugStateError(
                "Read between 1 and 1000 instructions with an integer offset."
            )
        body = await self.request(
            "disassemble",
            {"memoryReference": native, "instructionOffset": offset, "instructionCount": count},
            timeout=timeout,
        )
        self.check_stop(reference)
        instructions = []
        for value in list_value(body.get("instructions", []))[:count]:
            item = object_value(value)
            instructions.append(
                m.Instruction(
                    address=string(item.get("address")),
                    instruction=string(item.get("instruction")),
                    instruction_bytes=cast(str | None, item.get("instructionBytes")),
                    symbol=cast(str | None, item.get("symbol")),
                    location=self.location(item) if "location" in item else None,
                )
            )
        return m.DisassemblyResult(snapshot=self.snapshot(), instructions=instructions)

    async def finish_close(self, timeout: float) -> m.CloseResult:
        """Attempt protocol cleanup, then always release the owned adapter transport."""
        error = None
        outcome = (
            "already_exited" if isinstance(self.state.status, m.ExitedStatus) else "unknown"
        )
        self.publish(phase=m.SessionPhase.CLOSING)
        if self.connection is not None:
            try:
                await self.request(
                    "disconnect",
                    {"terminateDebuggee": self.state.mode == "launch"},
                    timeout=timeout,
                )
                if outcome != "already_exited":
                    outcome = {
                        "launch": "terminated",
                        "attach": "detached",
                        "dump": "released",
                    }.get(
                        self.state.mode,
                        "unknown",
                    )
            except (DebugError, ToolchainError, OSError) as failure:
                error = str(failure)
            finally:
                if self.reader is not None:
                    self.reader.cancel()
                    with suppress(asyncio.CancelledError):
                        await self.reader
                try:
                    await self.lifetime.aclose()
                except (ToolchainError, OSError) as failure:
                    error = error or str(failure)
                self.connection = None
        else:
            await self.lifetime.aclose()
        if self.state.mode == "dump":
            # A dump has no live program to detach; closed transport releases it
            # even if this adapter does not acknowledge disconnect for core files.
            outcome = "released"
        self.generation += 1
        self.handles.clear()
        self.handle_ids.clear()
        self.publish(phase=m.SessionPhase.CLOSED, inspection_generation=None)
        return m.CloseResult(
            adapter_closed=True,
            target_outcome=outcome,
            error=error,
            snapshot=self.snapshot(),
        )

    async def close(self, *, timeout: float | None = None) -> m.CloseResult:
        """Complete cleanup even when the calling request is cancelled, and reuse its outcome."""
        limit = self.deadline(timeout, self.timeouts.shutdown)
        if self.close_task is None:
            self.close_task = asyncio.create_task(self.finish_close(limit))
        cancelled = False
        while not self.close_task.done():
            try:
                await asyncio.shield(self.close_task)
            except asyncio.CancelledError:
                cancelled = True
        result = self.close_task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result.model_copy(update={"snapshot": self.snapshot()}, deep=True)
