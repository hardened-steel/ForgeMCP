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
async def test_named_resources_are_immutable_and_failures_preserve_file_result(cpp_acceptance_project):
    workspace = WorkspaceService(cpp_acceptance_project)
    metadata = {"messages": ["original"]}
    calls = []

    async def provider(context):
        calls.append(context)
        return ExtensionOutput(
            resource=ExtensionResource("diagnostics.json", "application/json", '{"message":"original"}'),
            metadata=metadata,
        )

    async def broken(context):
        raise RuntimeError("private failure details")

    workspace.register_extension("diagnostics", provider)
    workspace.register_extension("broken", broken)
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
        assert "extensions_uri" not in result.structured_content
        resources = result.structured_content["resources"]
        assert set(resources) == {"diagnostics"}
        uri = resources["diagnostics"]["uri"]
        first = (await client.read_resource(uri)).contents[0].text
        metadata["messages"].append("changed")
        assert resources["diagnostics"]["messages"] == ["original"]
        assert (await client.read_resource(uri)).contents[0].text == first
        assert json.loads(first) == {"message": "original"}
        assert len(calls) == 1
        path = WorkspacePath("project/README.md")
        assert calls[0].texts[path] == result.structured_content["text"]
        with pytest.raises(MCPError):
            await client.read_resource(uri.replace("diagnostics.json", "extensions.json"))


@pytest.mark.anyio
async def test_cancelled_provider_does_not_publish_result(cpp_acceptance_project):
    workspace = WorkspaceService(cpp_acceptance_project)

    async def cancelled(context):
        raise asyncio.CancelledError()

    workspace.register_extension("cancel", cancelled)
    path = WorkspacePath("project/README.md")
    with pytest.raises(asyncio.CancelledError):
        await workspace.enrich_result("read", workspace.read_file(path), [path])
    assert not workspace.result_resources


@pytest.mark.anyio
async def test_provider_scope_does_not_enrich_other_tools(cpp_acceptance_project):
    workspace = WorkspaceService(cpp_acceptance_project)
    calls = []

    async def provider(context):
        calls.append(context.tool_name)
        return ExtensionOutput(resource=ExtensionResource("scoped.json", "application/json", "{}"))

    workspace.register_extension("scoped", provider, tools=("workspace_write_file",))
    path = WorkspacePath("project/README.md")
    result = workspace.read_file(path)
    read = await workspace.enrich_result("workspace_read_file", result, [path])
    assert read.resources == {} and calls == []
    assert workspace.extensions_for("workspace_search") == {}
    await workspace.enrich_result("workspace_write_file", result, [path])
    assert calls == ["workspace_write_file"]


@pytest.mark.anyio
@pytest.mark.parametrize("metadata", [{"uri": "https://example.com"}, {"mime_type": "text/plain"}])
async def test_provider_cannot_override_resource_link(cpp_acceptance_project, metadata):
    workspace = WorkspaceService(cpp_acceptance_project)

    async def provider(context):
        return ExtensionOutput(
            resource=ExtensionResource("example.json", "application/json", "{}"),
            metadata=metadata,
        )

    workspace.register_extension("invalid", provider)
    path = WorkspacePath("project/README.md")
    result = await workspace.enrich_result("read", workspace.read_file(path), [path])
    assert result.resources == {} and workspace.result_resources == {}
