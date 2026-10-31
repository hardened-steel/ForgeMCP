"""Scripted DAP sessions exercise domain state, references, lifecycle, and cancellation."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from pydantic import ValidationError

from forgemcp.debugger import models as m
from forgemcp.debugger.errors import (
    DebugStaleReferenceError,
    DebugStateError,
    DebugTimeoutError,
    DebugUnsupportedError,
)
from forgemcp.debugger.lldb_dap import LldbDapSession
from forgemcp.toolchain.spec import ToolKind, ToolSpec
from forgemcp.toolchain.tools.lldb_dap import DapEvent, DapRequest, DapResponse
from forgemcp.workspace.path import WorkspacePath


@pytest.fixture
def anyio_backend():
    """Use the production asyncio session implementation."""
    return "asyncio"


class Peer:
    """A deterministic adapter whose launch response waits for configurationDone."""

    # These independent controls model adapter timing and lifecycle for direct session tests.
    # pylint: disable=too-many-instance-attributes

    def __init__(self) -> None:
        """Prepare capabilities, bounded response data, and controlled event delivery."""
        self.process_id = 17
        self.queue = asyncio.Queue()
        self.sent = []
        self.opened = False
        self.closed = False
        self.start_request = None
        self.ignore = set()
        self.stops = True
        self.seen = {}
        self.capabilities = {
            "supportsConfigurationDoneRequest": True,
            "supportsFunctionBreakpoints": True,
            "supportsExceptionInfoRequest": True,
            "supportsModulesRequest": True,
            "supportsReadMemoryRequest": True,
            "supportsDisassembleRequest": True,
            "supportsVariablePaging": False,
            "exceptionBreakpointFilters": [
                {"filter": "cpp_throw", "label": "C++ throw", "default": True}
            ],
        }

    async def messages(self) -> AsyncGenerator[DapRequest | DapResponse | DapEvent]:
        """Yield one continuous transport stream until its context is closed."""
        while True:
            message = await self.queue.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        """End the owned fake transport without changing an external target."""
        self.closed = True
        self.queue.put_nowait(None)

    def response(self, request: DapRequest, body=None) -> None:
        """Queue an acknowledgement with the request's exact correlation identity."""
        self.queue.put_nowait(DapResponse(200, request.seq, request.command, True, body))

    def stopped(self, reason="breakpoint") -> None:
        """Publish a fully suspended target event at the fixture thread."""
        self.queue.put_nowait(
            DapEvent(
                201,
                "stopped",
                {"reason": reason, "threadId": 7, "allThreadsStopped": True},
            ),
        )

    async def send(self, message) -> None:
        """Respond to domain requests while retaining exact messages for assertions."""
        # Keep the fake wire interpreter explicit alongside its fixed response payloads.
        # pylint: disable=too-many-branches
        self.sent.append(message)
        if not isinstance(message, DapRequest):
            return
        self.seen.setdefault(message.command, asyncio.Event()).set()
        if message.command in self.ignore:
            return
        if message.command == "initialize":
            self.response(message, self.capabilities)
        elif message.command in ("launch", "attach"):
            self.start_request = message
            self.queue.put_nowait(DapEvent(202, "initialized", {}))
        elif message.command == "configurationDone":
            self.response(message)
            self.response(self.start_request)
            self.queue.put_nowait(DapEvent(203, "process", {"systemProcessId": 1234}))
            if self.stops:
                self.stopped()
        elif message.command == "setBreakpoints":
            self.response(message, {"breakpoints": [{"verified": True, "line": 12}]})
        elif message.command == "setFunctionBreakpoints":
            self.response(
                message,
                {"breakpoints": [{"verified": False, "message": "Pending symbols"}]},
            )
        elif message.command in ("continue", "next", "stepIn", "stepOut", "pause"):
            self.response(message)
            if self.stops:
                self.stopped("step" if message.command != "pause" else "pause")
        elif message.command == "threads":
            self.response(message, {"threads": [{"id": 7, "name": "main"}]})
        elif message.command == "stackTrace":
            self.response(
                message,
                {
                    "stackFrames": [
                        {
                            "id": 31,
                            "name": "debug_leaf",
                            "source": {"path": "debug/debug_main.cpp"},
                            "line": 12,
                            "column": 4,
                            "instructionPointerReference": "0xffffffffffffffff",
                        }
                    ],
                    "totalFrames": 1,
                },
            )
        elif message.command == "scopes":
            self.response(
                message,
                {"scopes": [{"name": "Locals", "variablesReference": 9, "expensive": False}]},
            )
        elif message.command == "variables":
            self.response(
                message,
                {
                    "variables": [
                        {
                            "name": "local_value",
                            "value": "42",
                            "type": "int",
                            "variablesReference": 0,
                        },
                        {
                            "name": "input",
                            "value": "40",
                            "type": "int",
                            "variablesReference": 0,
                        },
                    ]
                },
            )
        elif message.command == "modules":
            self.response(
                message,
                {
                    "modules": [{"name": "fixture_debug", "path": "debug/debug_main.cpp"}],
                    "totalModules": 1,
                },
            )
        elif message.command == "exceptionInfo":
            self.response(
                message,
                {
                    "exceptionId": "access_violation",
                    "breakMode": "unhandled",
                    "description": "fault",
                },
            )
        elif message.command == "readMemory":
            self.response(
                message,
                {
                    "address": "0xffffffffffffffff",
                    "unreadableBytes": message.arguments["count"],
                },
            )
        elif message.command == "disassemble":
            self.response(
                message,
                {"instructions": [{"address": "0xffffffffffffffff", "instruction": "ret"}]},
            )
        else:
            self.response(message)


@pytest.fixture(name="scenario")
def scenario_fixture(cpp_acceptance_project):
    """Bind a fake ToolSpec to the complete isolated acceptance workspace."""
    peer = Peer()

    @asynccontextmanager
    async def connect(cwd: Path) -> AsyncGenerator[Peer]:
        """Supply one owned peer while recording context cleanup."""
        assert cwd == cpp_acceptance_project
        peer.opened = True
        try:
            yield peer
        finally:
            await peer.close()

    tool = ToolSpec(
        "lldb-dap",
        ToolKind.DEBUGGER,
        cpp_acceptance_project / "lldb-dap.exe",
        {"connect": connect},
    )
    session = LldbDapSession(
        "debug-session",
        tool,
        project_root=cpp_acceptance_project,
        storage_root=cpp_acceptance_project.parent / "storage",
        timeouts=m.DebugTimeouts(request=0.3, startup=1, shutdown=0.1),
    )
    return session, peer, cpp_acceptance_project


def options() -> m.LaunchOptions:
    """Select an existing fixture input; the scripted adapter never executes it."""
    return m.LaunchOptions(executable=WorkspacePath("project/debug/debug_main.cpp"))


def token(session: LldbDapSession) -> m.StopToken:
    """Capture the target's current inspection generation for direct API calls."""
    snapshot = session.snapshot()
    return m.StopToken(
        session_id=snapshot.session_id,
        generation=snapshot.inspection_generation,
    )


@pytest.mark.anyio
async def test_initial_configuration_precedes_launch_response_and_early_stop_is_retained(
    scenario,
):
    """Complete DAP's two-phase startup without deadlock or losing its early stop."""
    session, peer, _ = scenario
    try:
        receipt = await session.launch(
            options(),
            m.BreakpointPlan(
                sources=[
                    m.SourceBreakpoints(
                        source=WorkspacePath("project/debug/debug_main.cpp"),
                        points=[m.SourceBreakpoint(line=11)],
                    )
                ],
                functions=["debug_leaf"],
            ),
        )
        result = await session.wait_for_stop(receipt.execution, timeout=0.2)
        assert result.outcome == "stopped"
        assert result.snapshot.status.kind == "stopped"
        assert result.snapshot.target_pid == 1234
        commands = [item.command for item in peer.sent if isinstance(item, DapRequest)]
        assert commands == [
            "initialize",
            "launch",
            "setBreakpoints",
            "setFunctionBreakpoints",
            "setExceptionBreakpoints",
            "configurationDone",
        ]
        assert session.snapshot().phase == m.SessionPhase.ACTIVE
        initialize = next(
            item for item in peer.sent
            if isinstance(item, DapRequest) and item.command == "initialize"
        )
        assert initialize.arguments["sourceInitFile"] is False
        assert not session.snapshot().capabilities.variable_paging
    finally:
        result = await session.close()
    assert result.target_outcome == "terminated" and peer.closed


@pytest.mark.anyio
async def test_inspection_memory_handles_and_step_invalidate_old_references(scenario):
    """Inspect real-shaped protocol data while keeping every reference stop-scoped."""
    session, _, _ = scenario
    try:
        receipt = await session.launch(options(), m.BreakpointPlan())
        await session.wait_for_stop(receipt.execution, timeout=0.2)
        thread = (await session.threads()).threads[0].reference
        stop = token(session)
        stack = await session.stack(thread, stop, m.PageRequest())
        frame = stack.frames[0]
        assert frame.location.path == "project/debug/debug_main.cpp"
        assert frame.location.address == "0xffffffffffffffff"
        scope = (await session.scopes(frame.reference)).scopes[0]
        values = await session.variables(scope.reference, m.PageRequest(count=1))
        assert values.variables[0].name == "local_value"
        assert values.next_start == 1
        tail = await session.variables(scope.reference, m.PageRequest(start=1, count=1))
        assert tail.variables[0].value == "40" and tail.next_start is None
        memory = await session.read_memory(frame.memory, 0, 8)
        assert memory.data is None and memory.unreadable_bytes == 8
        instructions = await session.disassemble(frame.memory, 0, 1)
        assert instructions.instructions[0].address == "0xffffffffffffffff"
        assert (await session.modules(m.PageRequest())).modules[0].name == "fixture_debug"
        assert (await session.exception_info(thread, stop)).exception_id == "access_violation"
        next_execution = await session.step(thread, stop, m.StepKind.OVER)
        assert (
            await session.wait_for_stop(next_execution.execution, timeout=0.2)
        ).outcome == "stopped"
        with pytest.raises(DebugStaleReferenceError):
            await session.scopes(frame.reference)
        with pytest.raises(DebugStaleReferenceError):
            await session.resume(stop)
    finally:
        await session.close()


@pytest.mark.anyio
async def test_wait_timeout_and_cancellation_preserve_session_and_later_stop(scenario):
    """Cancel only a waiter, leaving its retained execution future and adapter alive."""
    session, peer, _ = scenario
    peer.stops = False
    try:
        receipt = await session.launch(options(), m.BreakpointPlan())
        assert (
            await session.wait_for_stop(receipt.execution, timeout=0.01)
        ).outcome == "timeout"
        waiting = asyncio.create_task(session.wait_for_stop(receipt.execution, timeout=5))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert session.snapshot().target_state == m.TargetState.RUNNING
        assert not peer.closed
        peer.stopped()
        assert (
            await session.wait_for_stop(receipt.execution, timeout=0.2)
        ).outcome == "stopped"
    finally:
        await session.close()


@pytest.mark.anyio
async def test_request_timeout_has_uncertain_outcome_without_closing_transport(scenario):
    """An unacknowledged Continue does not invite an automatic repeat or kill."""
    session, peer, _ = scenario
    try:
        receipt = await session.launch(options(), m.BreakpointPlan())
        await session.wait_for_stop(receipt.execution, timeout=0.2)
        peer.ignore.add("continue")
        with pytest.raises(DebugTimeoutError) as failure:
            await session.resume(token(session), timeout=0.01)
        assert failure.value.outcome_unknown
        assert session.snapshot().target_state == m.TargetState.UNKNOWN
        assert not peer.closed
        peer.stopped()
        await asyncio.sleep(0)
        assert session.snapshot().target_state == m.TargetState.STOPPED
    finally:
        await session.close()


@pytest.mark.anyio
async def test_dump_inspection_and_close_never_request_live_execution(scenario):
    """Use coreFile attach and make dump-mode control errors explicit."""
    session, peer, root = scenario
    dump = root / "fixture.dmp"
    dump.write_bytes(b"scripted dump")
    try:
        snapshot = await session.open_dump(
            m.DumpOptions(dump_file=WorkspacePath("project/fixture.dmp"))
        )
        assert snapshot.target_state == m.TargetState.DUMP
        commands = [item.command for item in peer.sent if isinstance(item, DapRequest)]
        assert "setExceptionBreakpoints" not in commands
        attached = next(
            item
            for item in peer.sent
            if isinstance(item, DapRequest) and item.command == "attach"
        )
        assert attached.arguments["coreFile"] == str(dump)
        with pytest.raises(DebugUnsupportedError):
            await session.resume(token(session))
    finally:
        closed = await session.close()
    assert closed.target_outcome == "released"
    assert await session.close() == closed


@pytest.mark.anyio
async def test_attach_close_detaches_and_failed_disconnect_is_not_confirmed(scenario):
    """Separate a successful detach acknowledgement from an unknown target outcome."""
    session, peer, _ = scenario
    receipt = await session.attach(m.AttachOptions(pid=1234), m.BreakpointPlan())
    await session.wait_for_stop(receipt.execution, timeout=0.2)
    peer.ignore.add("disconnect")
    result = await session.close(timeout=0.01)
    assert result.adapter_closed and result.target_outcome == "unknown" and result.error
    disconnect = next(
        item
        for item in peer.sent
        if isinstance(item, DapRequest) and item.command == "disconnect"
    )
    assert disconnect.arguments["terminateDebuggee"] is False
    assert peer.closed


@pytest.mark.anyio
async def test_adapter_failure_has_one_status_variant_and_unknown_target(scenario):
    """A disconnected adapter is not evidence that its program exited."""
    session, peer, _ = scenario
    try:
        receipt = await session.launch(options(), m.BreakpointPlan())
        await session.wait_for_stop(receipt.execution, timeout=0.2)
        peer.queue.put_nowait(None)
        await session.reader
        snapshot = session.snapshot()
        assert snapshot.phase == m.SessionPhase.FAILED
        assert snapshot.target_state == m.TargetState.UNKNOWN
        assert isinstance(snapshot.status, m.FailedStatus)
        assert {"stop", "exit_code", "error"}.isdisjoint(snapshot.model_dump())
    finally:
        await session.close()


@pytest.mark.anyio
async def test_foreign_references_and_unsupported_capabilities_fail_before_io(scenario):
    """Check contract boundaries before sending a malformed or unsupported request."""
    session, peer, _ = scenario
    peer.capabilities["supportsModulesRequest"] = False
    try:
        receipt = await session.launch(options(), m.BreakpointPlan())
        await session.wait_for_stop(receipt.execution, timeout=0.2)
        with pytest.raises(DebugUnsupportedError):
            await session.modules(m.PageRequest())
        with pytest.raises(DebugStaleReferenceError):
            await session.resume(m.StopToken(session_id="other", generation=1))
        with pytest.raises(DebugStateError):
            await session.set_exception_breakpoints(["invented-filter"])
        assert not any(
            isinstance(item, DapRequest) and item.command == "modules" for item in peer.sent
        )
    finally:
        await session.close()


@pytest.mark.anyio
async def test_startup_cancellation_releases_owned_connection(scenario):
    """An unpublished incomplete start is cleaned up when its owner is cancelled."""
    session, peer, _ = scenario
    peer.ignore.add("launch")
    task = asyncio.create_task(session.launch(options(), m.BreakpointPlan()))
    while "launch" not in peer.seen:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert peer.closed
    assert session.snapshot().phase == m.SessionPhase.FAILED
    closed = await session.close()
    assert closed.snapshot == session.snapshot()


@pytest.mark.anyio
async def test_symbol_commands_reject_unsafe_configured_root_names(scenario):
    """Reject command substitution even when it occurs in an operator-configured root."""
    session, peer, root = scenario
    unsafe = root / "symbol`expression`"
    unsafe.mkdir()
    session.project_root = unsafe
    with pytest.raises(DebugStateError, match="backticks"):
        session.source_arguments(
            m.SourceOptions(symbol_directories=[WorkspacePath("project/")]),
        )
    assert not peer.opened


@pytest.mark.anyio
async def test_paths_cannot_escape_managed_roots_before_adapter_launch(scenario):
    """Reject explicit external inputs without starting a process or asking the adapter."""
    session, peer, root = scenario
    with pytest.raises(DebugStateError):
        await session.launch(
            m.LaunchOptions(
                executable=WorkspacePath("root/" + (root / "debug/debug_main.cpp").as_posix())
            ),
            m.BreakpointPlan(),
        )
    assert not peer.opened


def test_status_union_and_bounded_options_are_validated():
    """Make contradictory status fields and invalid budgets impossible in input schemas."""
    with pytest.raises(ValidationError):
        m.SessionSnapshot(session_id="s", exit_code=0)
    with pytest.raises(ValidationError):
        m.SessionSnapshot(
            session_id="s",
            status={"kind": "stopped", "reason": "step", "exit_code": 0},
        )
    with pytest.raises(ValidationError):
        m.PageRequest(count=1001)
    with pytest.raises(ValidationError):
        m.DebugTimeouts(request=float("nan"))
    with pytest.raises(ValidationError):
        m.LaunchOptions(executable=WorkspacePath("project/main"), arguments=["bad\0arg"])
