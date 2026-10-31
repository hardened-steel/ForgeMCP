"""Debugger registration, typed SDK results, registry, and runtime ownership."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import Client
from mcp.client import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID, Apps
from pydantic import ValidationError

from forgemcp.admin.history import MemoryHistory
from forgemcp.completion import Complete
from forgemcp.debugger import models as m
from forgemcp.debugger.errors import DebugAdapterError, DebugStateError
from forgemcp.debugger.service import DebuggerService
from forgemcp.server import ForgeMCPServer, ServerRuntime
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.spec import ToolKind, Toolset, ToolSpec


@pytest.fixture
def anyio_backend():
    """Use the server's asyncio lifecycle and in-process SDK transport."""
    return "asyncio"


class ScriptedSession:
    """Observe service dispatch without launching a native debugger."""

    def __init__(self, identifier: str, tool: ToolSpec) -> None:
        """Retain factory inputs and a newly created immutable observation."""
        self.tool = tool
        self.state = m.SessionSnapshot(session_id=identifier)
        self.calls = []
        self.fail_launch = False
        self.closed = 0

    def snapshot(self) -> m.SessionSnapshot:
        """Return an independent copy of the retained observation."""
        return self.state.model_copy(deep=True)

    async def launch(self, options, initial, *, timeout=None) -> m.ExecutionReceipt:
        """Record parsed options and return a completed initial breakpoint stop."""
        self.calls.append((options, initial, timeout))
        if self.fail_launch:
            raise DebugAdapterError("Scripted launch failed.")
        self.state = self.state.model_copy(
            update={
                "mode": "launch",
                "phase": m.SessionPhase.ACTIVE,
                "target_state": m.TargetState.STOPPED,
                "revision": 1,
                "inspection_generation": 1,
                "adapter_process_id": 17,
                "status": m.StoppedStatus(reason="breakpoint", all_threads_stopped=True),
            },
        )
        return m.ExecutionReceipt(
            execution=m.ExecutionRef(
                session_id=self.state.session_id,
                execution_id="initial",
            ),
            snapshot=self.snapshot(),
        )

    async def wait_for_stop(self, execution, *, timeout) -> m.WaitResult:
        """Return the already observed stop without coupling it to close."""
        assert execution.session_id == self.state.session_id
        assert timeout > 0
        return m.WaitResult(outcome="stopped", snapshot=self.snapshot())

    async def close(self, *, timeout=None) -> m.CloseResult:
        """Record explicit cleanup and produce a typed final observation."""
        del timeout
        self.closed += 1
        self.state = self.state.model_copy(
            update={"phase": m.SessionPhase.CLOSED},
        )
        return m.CloseResult(
            adapter_closed=True,
            target_outcome="terminated",
            snapshot=self.snapshot(),
        )


@pytest.fixture(name="setup")
def debugger_setup(cpp_acceptance_project: Path):
    """Compose a supported adapter, an unrelated toolset, and observable session creation."""
    tool = ToolSpec(
        "lldb-dap",
        ToolKind.DEBUGGER,
        cpp_acceptance_project / "lldb-dap.exe",
        {},
    )
    chains = ToolchainService(SimpleNamespace())
    chains.toolsets = (
        Toolset("system", "System", (tool,), None, True),
        Toolset("empty", "Empty", (), None, True),
    )
    created = []

    def factory(identifier, selected):
        """Return one scripted session for each concrete factory invocation."""
        session = ScriptedSession(identifier, selected)
        created.append(session)
        return session

    service = DebuggerService(chains, {"lldb-dap": factory}, progress_interval=0)
    apps = Apps()
    server = ForgeMCPServer("debugger-test", extensions=[apps])
    service.register(server, apps, Complete())
    for binding in apps.tools():
        server.add_tool(binding.fn, meta=binding.meta, **binding.kwargs)
    for binding in apps.resources():
        server.add_resource(binding.resource)
    return SimpleNamespace(service=service, server=server, created=created, tool=tool)


@pytest.mark.anyio
async def test_all_debugger_tools_have_typed_apps_icons_and_progress(setup):
    """Expose every public operation with structured data and useful plain text."""
    updates = []

    async def progress(value, total, message):
        """Retain per-request monotonic progress notifications."""
        updates.append((value, total, message))

    async with Client(
        setup.server,
        extensions=[advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})],
    ) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == {
            "debugger_adapters",
            "debugger_sessions",
            "debugger_get",
            "debugger_launch",
            "debugger_attach",
            "debugger_open_dump",
            "debugger_breakpoints",
            "debugger_function_breakpoints",
            "debugger_exception_breakpoints",
            "debugger_continue",
            "debugger_step",
            "debugger_pause",
            "debugger_wait",
            "debugger_threads",
            "debugger_stack",
            "debugger_scopes",
            "debugger_variables",
            "debugger_modules",
            "debugger_exception_info",
            "debugger_read_memory",
            "debugger_disassemble",
            "debugger_close",
        }
        for tool in tools:
            assert tool.output_schema and tool.icons and tool.meta["ui"]["resourceUri"]
            assert "ctx" not in tool.input_schema.get("properties", {})
            app = await client.read_resource(tool.meta["ui"]["resourceUri"])
            assert app.contents[0].mime_type == APP_MIME_TYPE
        result = await client.call_tool(
            "debugger_adapters",
            {},
            progress_callback=progress,
        )
    assert not result.is_error
    assert result.structured_content == {
        "result": [
            {"adapter": "lldb-dap", "toolset_id": "system", "toolset_name": "System"},
        ],
    }
    assert "lldb-dap" in result.content[0].text
    assert not result.content[0].text.lstrip().startswith("{")
    assert [value for value, _, _ in updates] == [0, 1]
    assert all(total == 1 for _, total, _ in updates)
    assert not setup.created


@pytest.mark.anyio
async def test_sdk_launch_wait_get_and_close_preserve_domain_values(setup):
    """Pass SDK options into the driver and retain one explicit session across requests."""
    async with Client(setup.server) as client:
        launched = await client.call_tool(
            "debugger_launch",
            {
                "toolset_id": "system",
                "options": {
                    "executable": "project/debug-app.exe",
                    "arguments": ["", "α value"],
                    "environment": {"VALUE": "two words"},
                },
                "initial": {
                    "sources": [
                        {"source": "project/debug/debug_main.cpp", "points": [{"line": 11}]},
                    ],
                },
                "timeout": 7,
            },
        )
        assert not launched.is_error, launched.content
        receipt = m.ExecutionReceipt.model_validate(launched.structured_content)
        identifier = receipt.snapshot.session_id
        session = setup.created[0]
        options, initial, timeout = session.calls[0]
        assert options.arguments == ["", "α value"]
        assert options.environment == {"VALUE": "two words"}
        assert initial.sources[0].points[0].line == 11
        assert timeout == 7
        state = await client.call_tool("debugger_get", {"session_id": identifier})
        assert state.structured_content["status"]["kind"] == "stopped"
        assert not {"stop", "exit_code", "error"}.intersection(state.structured_content)
        assert "breakpoint" in state.content[0].text
        waited = await client.call_tool(
            "debugger_wait",
            {
                "session_id": identifier,
                "execution": receipt.execution.model_dump(),
                "timeout": 1,
            },
        )
        assert waited.structured_content["outcome"] == "stopped"
        assert session.closed == 0
        closed = await client.call_tool("debugger_close", {"session_id": identifier})
        assert closed.structured_content["target_outcome"] == "terminated"
        assert session.closed == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments,explanation",
    [
        ({"toolset_id": "missing"}, "Unknown toolset"),
        ({"toolset_id": "empty"}, "has no lldb-dap adapter"),
        ({"toolset_id": "system", "adapter": "unsupported"}, "Unsupported debugger adapter"),
    ],
)
async def test_sdk_rejects_unavailable_selections_without_creating_sessions(
    setup,
    arguments,
    explanation,
):
    """Require an explicitly available supported ToolSpec before allocating a session."""
    async with Client(setup.server) as client:
        result = await client.call_tool(
            "debugger_launch",
            {**arguments, "options": {"executable": "project/app.exe"}},
        )
    assert result.is_error
    assert explanation in result.content[0].text
    assert not setup.created and not setup.service.sessions


@pytest.mark.anyio
async def test_registry_pages_do_not_change_sessions_and_unknown_ids_fail(setup):
    """Inspect cached snapshots with bounded windows and explicit identity errors."""
    first = setup.service.create_session("system", "lldb-dap")
    second = setup.service.create_session("system", "lldb-dap")
    assert first.tool is setup.tool
    assert first.snapshot().session_id != second.snapshot().session_id
    async with Client(setup.server) as client:
        result = await client.call_tool("debugger_sessions", {"page": {"start": 0, "count": 1}})
        assert len(result.structured_content["sessions"]) == 1
        assert result.structured_content["next_start"] == 1
        tail = await client.call_tool("debugger_sessions", {"page": {"start": 1, "count": 1}})
        assert tail.structured_content["next_start"] is None
        unknown = await client.call_tool("debugger_get", {"session_id": "missing"})
        assert unknown.is_error and "Unknown debugger session" in unknown.content[0].text
    await setup.service.close()
    assert all(session.closed == 1 for session in setup.created)
    with pytest.raises(DebugStateError, match="has closed"):
        setup.service.create_session("system", "lldb-dap")


@pytest.mark.anyio
async def test_failed_startup_cleans_up_the_registered_session(setup):
    """A recoverable driver startup error cannot leave an unowned running session."""
    normal_factory = setup.service.factories["lldb-dap"]

    def failing_factory(identifier, tool):
        """Make this session fail after registry allocation but before successful startup."""
        session = normal_factory(identifier, tool)
        session.fail_launch = True
        return session

    setup.service.factories["lldb-dap"] = failing_factory
    async with Client(setup.server) as client:
        result = await client.call_tool(
            "debugger_launch",
            {"toolset_id": "system", "options": {"executable": "project/app.exe"}},
        )
    assert result.is_error and "Scripted launch failed" in result.content[0].text
    assert setup.created[0].closed == 1
    assert setup.created[0].snapshot().phase == m.SessionPhase.CLOSED


@pytest.mark.parametrize("field", ["stop", "exit_code", "error"])
def test_snapshot_rejects_old_mutually_exclusive_top_level_fields(field):
    """Keep stop, exit, and failure details inside the single discriminated status field."""
    with pytest.raises(ValidationError):
        m.SessionSnapshot.model_validate({"session_id": "session", field: None})


@pytest.mark.parametrize(
    "status",
    [
        {"kind": "stopped", "reason": "breakpoint", "exit_code": 0},
        {"kind": "exited", "exit_code": 0, "message": "failure"},
        {"kind": "failed", "message": "adapter failed", "reason": "breakpoint"},
    ],
)
def test_snapshot_status_cannot_mix_stop_exit_and_failure_details(status):
    """Reject contradictory payloads instead of allowing multiple status variants at once."""
    with pytest.raises(ValidationError):
        m.SessionSnapshot.model_validate({"session_id": "session", "status": status})


@pytest.mark.anyio
async def test_runtime_closes_debugger_only_after_last_lifespan_user():
    """Release debugger targets before analysis and processes after shared ownership ends."""
    order = []

    async def close_debugger():
        """Record target cleanup while its process transport remains usable."""
        order.append("debugger")

    async def close_analysis():
        """Record language-server cleanup before shared process shutdown."""
        order.append("analysis")

    async def close_processes():
        """Record the final transport shutdown."""
        order.append("processes")

    runtime = ServerRuntime(
        workspace=SimpleNamespace(),
        processes=SimpleNamespace(close=close_processes),
        toolchains=SimpleNamespace(initialize=AsyncMock()),
        builds=SimpleNamespace(configure=AsyncMock(return_value=[])),
        analysis=SimpleNamespace(initialize=AsyncMock(), close=close_analysis),
        history=MemoryHistory(),
        settings={},
        debugger=SimpleNamespace(close=close_debugger),
    )
    async with runtime.lifespan():
        async with runtime.lifespan():
            assert runtime.users == 2
        assert runtime.users == 1 and not order
    assert order == ["debugger", "analysis", "processes"]
    assert runtime.closed


def test_session_budget_rejects_new_factory_work(setup, monkeypatch):
    """Bound retained sessions before another adapter instance can be created."""
    monkeypatch.setattr(setup.service, "MAX_SESSIONS", 1)
    setup.service.create_session("system", "lldb-dap")
    with pytest.raises(DebugStateError, match="session limit"):
        setup.service.create_session("system", "lldb-dap")
    assert len(setup.created) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("count", [0, 2, 9])
async def test_unknown_session_errors_use_retained_bounded_catalogs(setup, count):
    """Explain empty and large registries while bounding untrusted IDs without new sessions."""
    for _ in range(count):
        setup.service.create_session("system", "lldb-dap")
    identifier = "\n" * 200 + "PRIVATE_SUFFIX"
    async with Client(setup.server) as client:
        result = await client.call_tool("debugger_get", {"session_id": identifier})
    text = result.content[0].text
    assert result.is_error and "session_id=" in text and "debugger_sessions()" in text
    assert "PRIVATE_SUFFIX" not in text and "\n" not in text
    assert len(setup.created) == count
    if count == 0:
        assert "No values are currently available" in text and "open a target first" in text
    elif count <= 8:
        assert all(key in text for key in setup.service.sessions)
    else:
        assert "9 available values" in text
        assert all(key not in text for key in setup.service.sessions)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "options,expected,private",
    [
        (
            {"arguments": ["ok", "PRIVATE_ARGUMENT\0"]},
            ["options.arguments[1]", "NUL", "Remove NUL"],
            "PRIVATE_ARGUMENT",
        ),
        (
            {"environment": {"PRIVATE=KEY": "PRIVATE_VALUE"}},
            ["options.environment", "nonempty", "cannot contain '='"],
            "PRIVATE",
        ),
        (
            {"environment": {"TOKEN": "PRIVATE_VALUE\0"}},
            ["options.environment", "NUL", "32768"],
            "PRIVATE_VALUE",
        ),
        (
            {"private_unknown": "PRIVATE_VALUE"},
            ["options.private_unknown", "Extra inputs"],
            "PRIVATE_VALUE",
        ),
    ],
)
async def test_sdk_option_errors_identify_fields_without_private_input(
    setup,
    options,
    expected,
    private,
):
    """Keep nested validation in the SDK and reject inputs before allocating a session."""
    async with Client(setup.server) as client:
        response = await client.call_tool(
            "debugger_launch",
            {
                "toolset_id": "system",
                "options": {"executable": "project/app.exe", **options},
            },
        )
    text = response.content[0].text
    assert response.is_error and all(value in text for value in expected)
    assert private not in text and not setup.created


@pytest.mark.parametrize("count", [0, 9])
def test_unsupported_adapter_catalog_is_bounded_and_does_not_probe(setup, count):
    """Explain unavailable implementations from retained factories without launching programs."""
    factory = setup.service.factories["lldb-dap"]
    setup.service.factories = {f"adapter-{index}": factory for index in range(count)}
    with pytest.raises(DebugStateError) as failure:
        setup.service.create_session("system", "unavailable")
    text = str(failure.value)
    assert "adapter='unavailable'" in text and "debugger_adapters()" in text
    assert ("No values" if count == 0 else "9 available values") in text
    assert not setup.created


@pytest.mark.anyio
async def test_unexpected_driver_error_keeps_sdk_sanitization_and_cleanup(setup, monkeypatch):
    """Do not turn an implementation bug into a raw actionable domain error."""
    monkeypatch.setattr(
        ScriptedSession,
        "launch",
        AsyncMock(side_effect=RuntimeError("PRIVATE_INTERNAL_ERROR")),
    )
    async with Client(setup.server) as client:
        result = await client.call_tool(
            "debugger_launch",
            {"toolset_id": "system", "options": {"executable": "project/app"}},
        )
    assert result.is_error and "PRIVATE_INTERNAL_ERROR" not in result.content[0].text
    assert setup.created[0].closed == 1
