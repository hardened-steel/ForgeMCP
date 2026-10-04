"""Workspace observer lifecycle and immutable result resources."""

import asyncio
import json

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.shared.exceptions import MCPError
from pydantic import TypeAdapter

from forgemcp.completion import Complete
from forgemcp.workspace.errors import WorkspaceError
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import ResultProvider, WorkspaceService


@pytest.fixture
def anyio_backend():
    """Run async tests on the asyncio backend used by the process and LSP services."""
    return "asyncio"


@pytest.fixture
def setup(cpp_acceptance_project):
    """Mount an isolated workspace service and its immutable result resources for provider tests."""
    workspace = WorkspaceService(cpp_acceptance_project)
    apps = Apps()
    mcp = MCPServer("providers", extensions=[apps])
    complete = Complete()
    workspace.register(mcp, apps, complete)
    for binding in apps.tools():
        mcp.add_tool(binding.fn, meta=binding.meta, **binding.kwargs)
    complete.register(mcp)
    return workspace, mcp


@pytest.mark.parametrize(
    "value",
    [
        "project",
        "src/a",
        "project/../a",
        "storage/C:/a",
        "project/a\\b",
        "root/relative",
        "root/C:/SDK/../header.h",
    ],
)
def test_qualified_path_rejects_ambiguous_or_escaping_values(value):
    """Verify qualified paths reject ambiguous syntax and traversal outside their area."""
    with pytest.raises(ValueError):
        WorkspacePath(value)


@pytest.mark.parametrize(
    "value",
    [
        "project/",
        "storage/build/debug",
        "root/C:/SDK/header.h",
        "root//usr/include/header.h",
    ],
)
def test_qualified_path_is_a_json_string(value):
    """Verify qualified paths serialize and validate as inline JSON strings."""
    adapter = TypeAdapter(WorkspacePath)
    assert json.loads(adapter.dump_json(WorkspacePath(value))) == value
    assert adapter.json_schema()["type"] == "string"
    assert WorkspacePath("project/").relative == "."


@pytest.mark.anyio
async def test_context_and_immutable_resource_belong_to_one_call(setup):
    """Verify provider context and immutable resources stay bound to one copied tool result."""
    workspace, mcp = setup
    calls = []
    token = object()

    class Provider(ResultProvider[object]):
        """A provider that captures call context and publishes an immutable JSON resource."""

        async def before_workspace_write_file(self, call_id, path, text):
            """Capture the call arguments before file creation and return the shared context
            token.
            """
            calls.append((call_id, path, text))
            assert not workspace.resolve_workspace_path(path).exists()
            return token

        async def after_workspace_write_file(self, call_id, context, result):
            """Validate the context and saved file, mutate the copied result, and publish a
            resource.
            """
            assert context is token and call_id == calls[0][0]
            assert result.action == "created"
            assert workspace.resolve_workspace_path(result.path).read_text() == "original"
            result.lines_added = 999  # A provider cannot mutate the actual result.
            return workspace.save_result_resource(
                call_id,
                "example",
                "application/json",
                '{"saved":true}',
            )

    workspace.register_provider("example", Provider())
    async with Client(mcp) as client:
        response = await client.call_tool(
            "workspace_write_file",
            {"path": "project/provider.txt", "text": "original"},
        )
        assert not response.is_error
        assert response.structured_content["lines_added"] == 1
        link = response.structured_content["resources"]["example"]
        assert link["mime_type"] == "application/json"
        first = (await client.read_resource(link["uri"])).contents[0].text
        workspace.resolve_path("provider.txt").write_text("later change")
        assert (await client.read_resource(link["uri"])).contents[0].text == first
        assert json.loads(first) == {"saved": True}
        assert len(calls) == 1
        with pytest.raises(MCPError):
            await client.read_resource(link["uri"].replace("example.json", "missing.json"))
    with pytest.raises(WorkspaceError, match="already saved"):
        workspace.save_result_resource(calls[0][0], "example", "text/markdown", "replacement")


@pytest.mark.anyio
async def test_opt_out_and_provider_failures_preserve_successful_operation(setup):
    """Verify opting out and failing provider hooks do not change a successful file operation."""
    workspace, mcp = setup
    after_calls = []

    class OptOut(ResultProvider):
        """A provider whose inherited before hook opts out of the invocation."""

        async def after_workspace_write_file(self, *args):
            """Record an unexpected after hook call for an opted-out provider."""
            after_calls.append("optout")

    class BrokenBefore(ResultProvider):
        """A provider that fails before the underlying write."""

        async def before_workspace_write_file(self, *args, **kwargs):
            """Raise a private before-hook failure to test provider isolation."""
            raise RuntimeError("private before failure")

    class BrokenAfter(ResultProvider[bool]):
        """A participating provider that fails after saving a result resource."""

        async def before_workspace_write_file(self, *args, **kwargs):
            """Return a false-valued context that still participates in the call."""
            return False  # Only None opts out.

        async def after_workspace_write_file(self, call_id, context, result):
            """Record context, save a resource, and raise a private after-hook failure."""
            after_calls.append(context)
            workspace.save_result_resource(call_id, "after", "application/json", "{}")
            raise RuntimeError("private after failure")

    workspace.register_provider("optout", OptOut())
    workspace.register_provider("before", BrokenBefore())
    workspace.register_provider("after", BrokenAfter())
    async with Client(mcp) as client:
        response = await client.call_tool(
            "workspace_write_file",
            {"path": "project/ok.txt", "text": "ok"},
        )
    assert not response.is_error
    assert response.structured_content["resources"] == {}
    assert workspace.resolve_path("ok.txt").read_text() == "ok"
    assert not workspace.result_resources
    assert after_calls == [False]


@pytest.mark.anyio
async def test_failed_tool_calls_error_and_keeps_original_file(setup):
    """Verify a failed edit calls provider cleanup and preserves both file and resource state."""
    workspace, mcp = setup
    workspace.resolve_path("edit.txt").write_text("original")
    errors = []

    class Provider(ResultProvider[str]):
        """A provider recording cleanup for a deliberately failed edit."""

        async def before_workspace_edit_file(
            self,
            call_id,
            path,
            old_text,
            new_text,
            replace_all,
        ):
            """Validate exact edit arguments and return a cleanup context."""
            assert (old_text, new_text, replace_all) == ("missing", "new", False)
            return "context"

        async def after_workspace_edit_file(self, *args):
            """Fail if a rejected edit incorrectly reaches its success hook."""
            pytest.fail("A failed operation must not call after")

        async def error(self, call_id, context):
            """Record the failed call's identifier and participating context."""
            errors.append((call_id, context))

    workspace.register_provider("observer", Provider())
    async with Client(mcp) as client:
        response = await client.call_tool(
            "workspace_edit_file",
            {"path": "project/edit.txt", "old_text": "missing", "new_text": "new"},
        )
    assert response.is_error
    assert len(errors) == 1 and errors[0][1] == "context"
    assert workspace.resolve_path("edit.txt").read_text() == "original"
    assert not workspace.result_resources


@pytest.mark.anyio
async def test_providers_run_in_parallel_and_share_call_id(setup):
    """Verify before and after hooks overlap and share the same invocation identifier."""
    workspace, mcp = setup
    before = [asyncio.Event(), asyncio.Event()]
    after = [asyncio.Event(), asyncio.Event()]
    ids = {}

    class Provider(ResultProvider[int]):
        """A barrier-based provider that proves hooks execute concurrently."""

        def __init__(self, index):
            """Retain the provider's index in the paired barrier state."""
            self.index = index

        async def before_workspace_mkdir(self, call_id, path):
            """Record the shared call ID and wait for the other provider's before hook."""
            ids[self.index] = call_id
            before[self.index].set()
            await before[1 - self.index].wait()
            return self.index

        async def after_workspace_mkdir(self, call_id, context, result):
            """Validate context and wait for the other provider's after hook."""
            assert context == self.index and call_id == ids[self.index]
            after[self.index].set()
            await after[1 - self.index].wait()

    for index in range(2):
        workspace.register_provider(f"p{index}", Provider(index), tools=("workspace_mkdir",))
    async with Client(mcp) as client:
        response = await asyncio.wait_for(
            client.call_tool("workspace_mkdir", {"path": "project/parallel"}),
            timeout=5,
        )
        read = await client.call_tool("workspace_read_file", {"path": "project/README.md"})
    assert not response.is_error and not read.is_error
    assert len(ids) == 2 and ids[0] == ids[1] and len(ids[0]) == 32
    assert all(event.is_set() for event in after)


@pytest.mark.anyio
async def test_lock_serializes_tasks_and_allows_nested_delegation(setup):
    """Verify workspace locking serializes tasks while allowing reentry by the owning task."""
    workspace, _ = setup
    entered = asyncio.Event()

    async def other_operation():
        """Mark entry only after acquiring the serialized workspace operation context."""
        async with workspace.serialized_operation():
            entered.set()

    async with workspace.serialized_operation():
        async with workspace.serialized_operation():
            assert workspace.operation_lock.locked()
        task = asyncio.create_task(other_operation())
        await asyncio.sleep(0)
        assert not entered.is_set()
    await asyncio.wait_for(task, timeout=5)
    assert entered.is_set() and workspace.operation_owner is None
