"""Native MCP completion dispatch and its authenticated browser route."""

from __future__ import annotations

import json
from typing import Any

from mcp import Client
from mcp.server import MCPServer
from mcp.types import Completion, CompletionArgument, CompletionContext, PromptReference
from mcp.types import ResourceTemplateReference
import pytest

from forgemcp.admin.history import MemoryHistory
from forgemcp.admin.instance import InstanceAgent
from forgemcp.admin.service import HubInstance, HubService
from forgemcp.process.host import ProcessIdentity


@pytest.fixture
def anyio_backend():
    """Use the asyncio backend required by the local administration bridge."""
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reference",
    [
        {"type": "ref/resource", "uri": "forgemcp://example{?path,kind}"},
        {"type": "ref/prompt", "name": "example"},
    ],
)
async def test_completion_uses_sdk_reference_and_context(reference):
    """Keep both standard reference kinds and dependent argument context intact."""
    mcp = MCPServer(name="admin-completion-test")
    received = []

    @mcp.completion()
    async def complete(
        ref: ResourceTemplateReference | PromptReference,
        argument: CompletionArgument,
        context: CompletionContext | None,
    ) -> Completion:
        """Return representative server-owned suggestions with pagination metadata."""
        received.append((ref, argument, context))
        return Completion(values=["project/src", "project/tests"], total=4, has_more=True)

    async def unexpected_bootstrap() -> None:
        """Reject an accidental attempt to start a native Hub during dispatch."""
        raise AssertionError("Completion dispatch must not bootstrap a Hub.")

    agent = InstanceAgent(
        mcp,
        MemoryHistory(),
        {"id": "completion-test"},
        state_snapshot=None,
        ensure_hub=unexpected_bootstrap,
        ipc_name="unused-completion-test",
    )
    async with Client(mcp, cache=None) as client:
        result = await agent.dispatch(
            client,
            "completion/complete",
            {
                "ref": reference,
                "argument": {"name": "path", "value": "project/"},
                "context": {"arguments": {"kind": "source"}},
            },
        )
    ref, argument, context = received[0]
    assert ref.model_dump(exclude_none=True) == reference
    assert argument.name == "path"
    assert argument.value == "project/"
    assert context.arguments == {"kind": "source"}
    assert result.completion.values == ["project/src", "project/tests"]
    assert result.completion.total == 4
    assert result.completion.has_more is True
    assert agent.history.list_calls() == []


async def completion_response(
    hub: HubService,
    body: Any,
    *,
    csrf: str | None,
) -> tuple[int, Any]:
    """Exercise Starlette routing with a browser request without a loopback socket."""
    payload = json.dumps(body).encode()
    messages = []

    async def receive():
        """Deliver one complete JSON request body to Starlette."""
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        """Retain the response status and JSON bytes written by the ASGI app."""
        messages.append(message)

    headers = [
        (b"host", b"127.0.0.1:8765"),
        (b"origin", b"http://127.0.0.1:8765"),
        (b"cookie", f"{hub.cookie_name}={hub.session_token}".encode()),
    ]
    if csrf is not None:
        headers.append((b"x-forgemcp-token", csrf.encode()))
    await hub.app()(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/instances/test/completion",
            "query_string": b"",
            "headers": headers,
            "server": ("127.0.0.1", 8765),
        },
        receive,
        send,
    )
    status = next(
        message["status"]
        for message in messages
        if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return status, json.loads(response_body)


@pytest.mark.anyio
async def test_completion_http_route_forwards_standard_payload(monkeypatch):
    """Forward native completion JSON only through the protected POST endpoint."""
    hub = HubService(None)
    hub.origin = "http://127.0.0.1:8765"
    hub.instances["test"] = HubInstance({"id": "test"}, ProcessIdentity(1, 1))
    received = []

    async def request(instance, method, params):
        """Capture routing without creating a native IPC connection."""
        received.append((instance, method, params))
        return {"completion": {"values": ["project/"], "hasMore": False}}

    monkeypatch.setattr(HubInstance, "request", request)
    params = {
        "ref": {"type": "ref/resource", "uri": "forgemcp://example{?path}"},
        "argument": {"name": "path", "value": "pro"},
        "context": {"arguments": {}},
    }
    status, response = await completion_response(hub, params, csrf=hub.csrf_token)
    assert status == 200
    assert received == [(hub.instances["test"], "completion/complete", params)]
    assert response["completion"]["values"] == ["project/"]


@pytest.mark.anyio
@pytest.mark.parametrize("csrf", [None, "incorrect"])
async def test_completion_http_rejects_missing_or_incorrect_csrf(csrf):
    """Apply the existing browser mutation protection to completion requests."""
    hub = HubService(None)
    hub.origin = "http://127.0.0.1:8765"
    status, response = await completion_response(hub, {}, csrf=csrf)
    assert status == 403
    assert response["error"] == "Invalid CSRF token."


@pytest.mark.anyio
async def test_completion_http_rejects_non_object_body():
    """Report malformed completion envelopes before sending them over IPC."""
    hub = HubService(None)
    hub.origin = "http://127.0.0.1:8765"
    hub.instances["test"] = HubInstance({"id": "test"}, ProcessIdentity(1, 1))
    status, response = await completion_response(hub, [], csrf=hub.csrf_token)
    assert status == 400
    assert "JSON object" in response["error"]
