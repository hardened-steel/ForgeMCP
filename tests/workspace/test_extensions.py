import asyncio
import json

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from forgemcp.completion import Complete
from forgemcp.workspace.extensions import ExtensionOutput, ExtensionResource
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import WorkspaceService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("value", ["project", "src/a", "project/../a", "storage/C:/a", "project/a\\b"])
def test_qualified_path_rejects_ambiguous_or_escaping_values(value):
    with pytest.raises(ValidationError):
        WorkspacePath(value)


def test_qualified_path_serializes_as_string():
    path = WorkspacePath("storage/build/debug")
    assert path.model_dump_json() == '"storage/build/debug"'
    assert (path.area, path.relative) == ("storage", "build/debug")
    assert WorkspacePath("project/").relative == "."


@pytest.mark.anyio
async def test_extensions_are_immutable_and_provider_failure_preserves_file_result(cpp_acceptance_project):
    workspace = WorkspaceService(cpp_acceptance_project)
    payload = {"messages": ["original"]}
    calls = []

    async def provider(context):
        calls.append(context)
        return ExtensionOutput(
            data=payload,
            resources=(ExtensionResource("diagnostics.md", "text/markdown", "original"),),
        )

    async def broken(context):
        raise RuntimeError("private failure details")

    workspace.register_extension("diagnostics", provider, kind="diagnostics")
    workspace.register_extension("broken", broken, kind="diagnostics")
    apps = Apps()
    mcp = MCPServer("extension-unit", extensions=[apps])
    complete = Complete()
    workspace.register(mcp, apps, complete)
    for binding in apps.tools():
        mcp.add_tool(binding.fn, meta=binding.meta, **binding.kwargs)
    complete.register(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool("workspace_read_file", {"path": "project/README.md"})
        assert not result.is_error
        uri = result.structured_content["extensions_uri"]
        first = (await client.read_resource(uri)).contents[0].text
        payload["messages"].append("changed")
        assert (await client.read_resource(uri)).contents[0].text == first
        manifest = json.loads(first)
        assert manifest["extensions"][0]["data"] == {"messages": ["original"]}
        assert manifest["extensions"][1]["error"] == "Extension provider failed."
        assert "private failure details" not in first
        extra = result.structured_content["resources"][0]["uri"]
        assert (await client.read_resource(extra)).contents[0].text == "original"
        assert len(calls) == 1
        path = WorkspacePath("project/README.md")
        assert calls[0].texts[path] == result.structured_content["text"]
        with pytest.raises(MCPError):
            await client.read_resource(uri.replace("extensions.json", "missing.json"))


@pytest.mark.anyio
async def test_cancelled_provider_does_not_publish_result(cpp_acceptance_project):
    workspace = WorkspaceService(cpp_acceptance_project)

    async def cancelled(context):
        raise asyncio.CancelledError()

    workspace.register_extension("cancel", cancelled, kind="test")
    path = WorkspacePath("project/README.md")
    with pytest.raises(asyncio.CancelledError):
        await workspace.enrich_result("read", workspace.read_file(path), [path])
    assert not workspace.result_resources
