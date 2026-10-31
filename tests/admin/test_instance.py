"""Admin IPC wire serialization shared with the browser's MCP Apps handshake tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.server import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import (
    CallToolResult,
    ElicitRequestFormParams,
    ErrorData,
    Icon,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import BaseModel
import pytest

from forgemcp.admin.history import CallProgress, MemoryHistory, ToolCall
from forgemcp.admin.instance import ElicitationPrompt, InstanceAgent, RpcResponse


class RecordingConnection:
    """Retain serialized bytes without opening a native IPC endpoint."""

    def __init__(self) -> None:
        """Create an empty capture of complete outbound frames."""
        self.payloads: list[bytes] = []

    async def send(self, payload: bytes) -> None:
        """Record the exact bytes that a real connection would frame."""
        self.payloads.append(payload)


@pytest.fixture
def anyio_backend():
    """Use the project's asyncio backend without a running MCP transport."""
    return "asyncio"


@pytest.fixture
def agent():
    """Compose only the SDK object and journal needed by the IPC serializer."""
    async def unexpected_bootstrap() -> None:
        """Fail if a serialization test accidentally tries to launch a Hub."""
        raise AssertionError("Serialization must not bootstrap an admin Hub.")

    return InstanceAgent(
        MCPServer(name="admin-wire-test"),
        MemoryHistory(),
        {"id": "wire-test"},
        state_snapshot=None,
        ensure_hub=unexpected_bootstrap,
        ipc_name="unused-wire-test",
    )


async def captured_payload(agent: InstanceAgent, message: BaseModel) -> dict[str, Any]:
    """Read one real serializer output through a connection with no OS resources."""
    connection = RecordingConnection()
    await agent.send(connection, message)
    assert len(connection.payloads) == 1
    return json.loads(connection.payloads[0])


def sample_tool() -> Tool:
    """Build a representative decorated-tool shape with unset optional SDK metadata."""
    return Tool(
        name="workspace_read_file",
        description="Read project UTF-8 text.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": None},
            },
        },
        output_schema={
            "type": "object",
            "properties": {
                "error": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                    "default": None,
                },
            },
        },
        icons=[
            Icon(
                src="data:image/svg+xml;base64,PHN2Zy8+",
                mime_type="image/svg+xml",
                sizes=["any"],
            ),
        ],
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        meta={"ui": {"resourceUri": "ui://forgemcp/workspace-file.html"}},
    )


def sample_result() -> CallToolResult:
    """Build an SDK response whose structured domain data intentionally contains null."""
    return CallToolResult(
        content=[TextContent(type="text", text="1 | int main() {}")],
        structured_content={
            "path": "project/main.cpp",
            "error": None,
            "process_id": None,
            "details": {"selected": None, "values": [None, "α"]},
        },
    )


@pytest.mark.anyio
async def test_tool_metadata_omits_absent_sdk_fields(agent):
    """Keep Tool/Icon/ToolAnnotations compatible with optional, non-nullable JS schemas."""
    payload = await captured_payload(
        agent,
        RpcResponse(id="tools", result={"tools": [sample_tool()]}),
    )
    tool = payload["result"]["tools"][0]
    assert "title" not in tool
    assert "execution" not in tool
    assert "theme" not in tool["icons"][0]
    assert tool["icons"][0]["mimeType"] == "image/svg+xml"
    assert tool["icons"][0]["sizes"] == ["any"]
    assert tool["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    assert tool["_meta"]["ui"]["resourceUri"].startswith("ui://")


@pytest.mark.anyio
async def test_result_omits_absent_protocol_metadata_and_retains_domain_nulls(agent):
    """Clean SDK envelopes while preserving the tool module's complete structured result."""
    result = sample_result()
    payload = await captured_payload(agent, RpcResponse(id="result", result=result))
    wire_result = payload["result"]
    assert "_meta" not in wire_result
    assert wire_result["content"] == [
        {"type": "text", "text": "1 | int main() {}"},
    ]
    assert wire_result["structuredContent"] == result.structured_content
    assert wire_result["isError"] is False


@pytest.mark.anyio
async def test_schema_defaults_and_argument_null_values_survive(agent):
    """Exclude only model-valued omissions, keeping meaningful null in plain JSON maps."""
    call = ToolCall(
        tool_name="workspace_read_file",
        arguments={"path": None, "nested": {"value": None}, "values": [None]},
    )
    payload = await captured_payload(
        agent,
        RpcResponse(id="snapshot", result={"tool": sample_tool(), "call": call}),
    )
    assert payload["result"]["call"]["arguments"] == call.arguments
    assert payload["result"]["tool"]["inputSchema"]["properties"]["path"]["default"] is None
    assert payload["result"]["tool"]["outputSchema"]["properties"]["error"]["default"] is None


@pytest.mark.anyio
async def test_history_defaults_survive_nested_serialization(agent):
    """Retain generated invocation IDs, UTC times and progress defaults on the wire."""
    call = ToolCall(tool_name="workspace_read_file", arguments={})
    call.progress.append(CallProgress(value=1))
    payload = await captured_payload(
        agent,
        RpcResponse(id="history", result={"history": [call]}),
    )
    wire_call = payload["result"]["history"][0]
    assert wire_call["id"] == call.id
    assert len(wire_call["id"]) == 32
    assert datetime.fromisoformat(wire_call["started_at"]) == call.started_at
    assert wire_call["status"] == "running"
    assert wire_call["source"] == "stdio"
    assert wire_call["progress"][0]["value"] == 1
    assert datetime.fromisoformat(wire_call["progress"][0]["timestamp"]) == call.progress[0].timestamp
    empty = ToolCall(tool_name="workspace_list", arguments={})
    payload = await captured_payload(agent, RpcResponse(id="empty", result=empty))
    assert payload["result"]["progress"] == []


@pytest.mark.anyio
async def test_elicitation_defaults_and_schema_null_survive(agent):
    """Preserve pending-question identity and time without invalid null SDK fields."""
    prompt = ElicitationPrompt(
        request_id="command",
        params=ElicitRequestFormParams(
            message="Allow external file read?",
            requested_schema={
                "type": "object",
                "properties": {"reason": {"type": "string", "default": None}},
            },
        ),
    )
    payload = await captured_payload(
        agent,
        RpcResponse(id="questions", result={"requests": [prompt]}),
    )
    wire_prompt = payload["result"]["requests"][0]
    assert wire_prompt["id"] == prompt.id
    assert datetime.fromisoformat(wire_prompt["created_at"]) == prompt.created_at
    assert wire_prompt["request_id"] == "command"
    assert "_meta" not in wire_prompt["params"]
    assert wire_prompt["params"]["requestedSchema"]["properties"]["reason"]["default"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("result", [None, {}, {"value": None}])
async def test_success_response_has_only_result_branch(agent, result):
    """Keep a successful null result explicit while removing the unused error branch."""
    payload = await captured_payload(agent, RpcResponse(id="success", result=result))
    assert payload == {"type": "response", "id": "success", "result": result}


@pytest.mark.anyio
async def test_error_response_has_only_error_branch(agent):
    """Keep SDK errors and omit their absent data and the unused result branch."""
    payload = await captured_payload(
        agent,
        RpcResponse(
            id=None,
            error=ErrorData(code=-32602, message="Invalid tool arguments."),
        ),
    )
    assert payload == {
        "type": "response",
        "id": None,
        "error": {"code": -32602, "message": "Invalid tool arguments."},
    }


@pytest.mark.anyio
async def test_wire_fixture_matches_real_sdk_serialization(agent):
    """Share actual serialized MCP metadata and results with the browser SDK tests."""
    instant = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    call = ToolCall(
        id="0123456789abcdef0123456789abcdef",
        tool_name="workspace_read_file",
        arguments={"path": "project/main.cpp", "end_line": None},
        started_at=instant,
        finished_at=instant,
        status="completed",
        result=sample_result(),
    )
    payload = await captured_payload(
        agent,
        RpcResponse(
            id="widget-snapshot",
            result={"tools": [sample_tool()], "history": [call]},
        ),
    )
    fixture = Path(__file__).parent / "fixtures" / "widget-wire.json"
    assert payload == json.loads(fixture.read_text(encoding="utf-8"))


@pytest.mark.anyio
async def test_debugger_tools_are_hidden_and_blocked_only_for_admin(request):
    """Reject crafted admin debugging calls while preserving ordinary and MCP client calls."""
    instance = request.getfixturevalue("agent")
    executed = []

    @instance.mcp.tool()
    async def debugger_launch() -> str:
        """Record whether debugging reached its SDK handler."""
        executed.append("debugger")
        return "debugger executed"

    @instance.mcp.tool()
    async def workspace_example() -> str:
        """Keep non-debugging admin commands available."""
        executed.append("workspace")
        return "workspace executed"

    async with Client(instance.mcp, read_timeout_seconds=5) as client:
        original = await client.list_tools()
        catalog = await instance.dispatch(client, "tools/list", {})
        snapshot = await instance.dispatch(client, "snapshot", {})
        assert {tool.name for tool in catalog.tools} == {"workspace_example"}
        assert {tool.name for tool in snapshot["tools"]} == {"workspace_example"}
        assert {tool.name for tool in original.tools} == {
            "debugger_launch",
            "workspace_example",
        }
        for name in ("debugger_launch", "debugger_sessions", "debugger_future_tool"):
            with pytest.raises(MCPError, match="unavailable through the admin panel"):
                await instance.dispatch(client, "tools/call", {"name": name})
        assert not executed
        ordinary = await instance.dispatch(
            client,
            "tools/call",
            {"name": "workspace_example"},
        )
        assert not ordinary.is_error
        direct = await client.call_tool("debugger_launch", {})
        assert not direct.is_error
        assert executed == ["workspace", "debugger"]


@pytest.mark.anyio
async def test_crafted_debugger_admin_request_returns_protocol_error(request):
    """Return the admin protocol error before any client method can be executed."""
    instance = request.getfixturevalue("agent")
    connection = RecordingConnection()
    await instance.handle_request(
        connection,
        None,
        {
            "id": "debugger-call",
            "method": "tools/call",
            "params": {"name": "debugger_step", "arguments": {}},
        },
    )
    assert json.loads(connection.payloads[0]) == {
        "type": "response",
        "id": "debugger-call",
        "error": {
            "code": -32601,
            "message": "Debugger tools are unavailable through the admin panel.",
        },
    }
