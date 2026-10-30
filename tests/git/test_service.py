"""Repository resource snapshots and real Git-to-CMake synchronization boundaries."""

import hashlib

from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import ResourceTemplateReference
import pytest

from forgemcp.cmake.file_api import ConfigurationInputs
from forgemcp.cmake.models import CompilationContext
from forgemcp.workspace.path import WorkspacePath


@pytest.mark.anyio
async def test_captured_resources_completions_and_monitor_do_not_launch_git(git_project):
    """Serve retained commit/status data and monitor state without synthetic tool calls."""
    runtime = git_project.runtime()
    async with Client(runtime.mcp) as client:
        with pytest.raises(MCPError):
            await client.read_resource("forgemcp://git/status")
        status = await client.call_tool("git_status", {})
        history = await client.call_tool("git_log", {})
        assert not status.is_error and not history.is_error
        oid = history.structured_content["commits"][0]["oid"]
        processes = len(runtime.processes.records)
        saved = await client.read_resource("forgemcp://git/status")
        assert "Last captured" in saved.contents[0].text
        commit = await client.read_resource("forgemcp://git/commits/" + oid)
        assert oid in commit.contents[0].text
        completion = await client.complete(
            ResourceTemplateReference(uri="forgemcp://git/commits/{revision}"),
            {"name": "revision", "value": oid[:8]},
        )
        assert oid in completion.completion.values
        state = await runtime.state_snapshot()
        assert state["git"]["last_status"]["head"] == oid
        assert len(runtime.processes.records) == processes


def confirmed_context(runtime, source):
    """Seed a confirmed build context without invoking a compiler or CMake executable."""
    directory = WorkspacePath("storage/build/debug")
    database = runtime.workspace.storage_root / "build/debug/compile_commands.json"
    database.parent.mkdir(parents=True)
    database.write_text("[]", encoding="utf-8")
    runtime.builds.inputs[directory] = ConfigurationInputs(
        checksums={
            WorkspacePath("project/CMakeLists.txt"): hashlib.sha256(
                source.read_bytes(),
            ).hexdigest(),
        },
        current=True,
    )
    runtime.builds.configurations["debug"] = CompilationContext(
        id="debug",
        toolset_id="system",
        build_directory=directory,
        compilation_database=WorkspacePath("storage/build/debug/compile_commands.json"),
    )


@pytest.mark.anyio
async def test_switch_withdraws_changed_cmake_inputs_through_subscription(git_project):
    """A real branch switch withdraws contexts using CMake-owned checks, not clangd shortcuts."""
    await git_project.command("branch", "before-settings")
    source = git_project.root / "CMakeLists.txt"
    source.write_text(
        source.read_text(encoding="utf-8") + "\n# Changed settings\n",
        encoding="utf-8",
    )
    await git_project.commit("Change CMake settings")
    runtime = git_project.runtime()
    confirmed_context(runtime, source)
    updates = runtime.builds.configuration_updates()
    assert len(await anext(updates)) == 1
    try:
        async with Client(runtime.mcp) as client:
            switched = await client.call_tool("git_switch", {"branch": "before-settings"})
            assert not switched.is_error
            assert switched.structured_content["error"] is None
            assert switched.structured_content["synchronization_error"] is None
            assert await anext(updates) == []
            assert await runtime.builds.compilation_contexts() == []
            assert not runtime.analysis.contexts
            assert not runtime.analysis.lock.locked()
            assert not runtime.builds.operation_lock.locked()
    finally:
        await updates.aclose()


@pytest.mark.anyio
async def test_ordinary_git_source_edit_preserves_cmake_context(git_project):
    """Source-only commits keep confirmed non-glob CMake inputs and release every feature lock."""
    runtime = git_project.runtime()
    confirmed_context(runtime, git_project.root / "CMakeLists.txt")
    target = git_project.root / "git-change.cpp"
    target.write_bytes(b"int added;\n")
    async with Client(runtime.mcp) as client:
        staged = await client.call_tool("git_stage", {"paths": ["project/git-change.cpp"]})
        assert staged.structured_content["error"] is None
        committed = await client.call_tool("git_commit", {"message": "Source only"})
        assert committed.structured_content["error"] is None
        assert len(await runtime.builds.compilation_contexts()) == 1
        assert not runtime.builds.operation_lock.locked()


@pytest.mark.anyio
async def test_secondary_analysis_failure_preserves_completed_commit(git_project, monkeypatch):
    """A failed source refresh must not hide a successful commit or encourage a duplicate retry."""
    target = git_project.root / "git-change.cpp"
    target.write_bytes(b"int added;\n")
    await git_project.command("add", "git-change.cpp")
    runtime = git_project.runtime()

    async def fail_refresh(_changes):
        """Model an unexpected secondary failure after the commit has already completed."""
        raise RuntimeError("private diagnostic detail")

    monkeypatch.setattr(runtime.analysis, "refresh_source_changes", fail_refresh)
    async with Client(runtime.mcp) as client:
        committed = await client.call_tool("git_commit", {"message": "Retain primary result"})
        assert not committed.is_error
        result = committed.structured_content
        assert result["error"] is None
        assert result["head_before"] != result["head_after"]
        assert "synchronization failed" in result["synchronization_error"]
        assert "private diagnostic detail" not in str(result)
