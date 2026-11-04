"""Repository resource snapshots and real Git-to-CMake synchronization boundaries."""

import asyncio
import hashlib
import stat
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import ResourceTemplateReference
import pytest

from forgemcp.clangd.service import RunningSession
from forgemcp.cmake.file_api import ConfigurationInputs
from forgemcp.cmake.models import CompilationContext
from forgemcp.git import parsers
from forgemcp.git.errors import GitError, GitParseError
from forgemcp.toolchain.spec import ToolKind, Toolset, ToolSpec
from forgemcp.toolchain.tools.git import CommandResult
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


def confirmed_context(runtime, source, *, name="debug", structural=False):
    """Seed a confirmed build context with an explicit structural-dependency policy."""
    directory = WorkspacePath(f"storage/build/{name}")
    database = runtime.workspace.storage_root / f"build/{name}/compile_commands.json"
    database.parent.mkdir(parents=True)
    database.write_text("[]", encoding="utf-8")
    runtime.builds.inputs[directory] = ConfigurationInputs(
        checksums={
            WorkspacePath("project/CMakeLists.txt"): hashlib.sha256(
                source.read_bytes(),
            ).hexdigest(),
        },
        structural=structural,
        current=True,
    )
    runtime.builds.configurations[name] = CompilationContext(
        id=name,
        toolset_id="system",
        build_directory=directory,
        compilation_database=WorkspacePath(f"{directory}/compile_commands.json"),
    )


@pytest.fixture(name="retained_analysis")
async def retained_analysis_fixture(
    git_project,
    monkeypatch,
) -> AsyncGenerator[SimpleNamespace, None]:
    """Use real Git and CMake checks with observable retained clangd session lifetimes."""
    runtime = git_project.runtime()
    selected = runtime.toolchains.toolsets[0]
    runtime.toolchains.toolsets = (
        Toolset(
            selected.id,
            selected.name,
            (
                *selected.tools,
                ToolSpec("clangd", ToolKind.LANGUAGE_SERVER, git_project.root / "clangd", {}),
            ),
            selected.environment,
            selected.inherit_environment,
        ),
    )
    for name, structural in (("debug", True), ("release", False)):
        confirmed_context(
            runtime,
            git_project.root / "CMakeLists.txt",
            name=name,
            structural=structural,
        )
    peers = {}

    async def retained_session(context, _on_progress, _timeout):
        """Substitute only language-server startup while retaining production lifecycle checks."""
        if context.id not in runtime.analysis.sessions:
            peer = SimpleNamespace(
                configuration=context,
                failure=None,
                notify=AsyncMock(),
                shutdown=AsyncMock(),
            )
            peers[context.id] = peer
            runtime.analysis.sessions[context.id] = RunningSession(
                peer,
                AsyncExitStack(),
                runtime.analysis.fingerprint(context),
            )
        return runtime.analysis.sessions[context.id].session

    monkeypatch.setattr(runtime.analysis, "session", retained_session)
    monkeypatch.setattr(runtime.builds, "configure", AsyncMock(return_value=[]))
    try:
        yield SimpleNamespace(runtime=runtime, peers=peers)
    finally:
        await runtime.close_services()


def assert_retained_sessions(scenario):
    """Require both original sessions and confirmed contexts to survive a Git operation."""
    assert set(scenario.runtime.analysis.sessions) == {"debug", "release"}
    for name, peer in scenario.peers.items():
        assert scenario.runtime.analysis.sessions[name].session is peer
        peer.shutdown.assert_not_awaited()
    assert all(snapshot.current for snapshot in scenario.runtime.builds.inputs.values())
    assert not scenario.runtime.analysis.lock.locked()
    assert not scenario.runtime.builds.operation_lock.locked()


@pytest.mark.anyio
async def test_index_and_ref_mutations_retain_structural_context_sessions(
    git_project,
    retained_analysis,
):
    """Staging, commits, and same-tree ref changes preserve physical source kinds and sessions."""
    source = git_project.root / "new α file.cpp"
    source.write_text("int added;\n", encoding="utf-8")
    source_path = "project/new α file.cpp"
    runtime = retained_analysis.runtime
    async with Client(runtime.mcp) as client:
        for tool, arguments in (
            ("git_stage", {"paths": [source_path]}),
            ("git_unstage", {"paths": [source_path]}),
            ("git_stage", {"paths": [source_path]}),
            ("git_commit", {"message": "Commit existing working files"}),
            ("git_commit", {"message": "Amend metadata", "amend": True}),
            ("git_branch", {"action": "create", "name": "same-tree"}),
            ("git_switch", {"branch": "same-tree"}),
            ("git_switch", {"branch": "main"}),
            ("git_branch", {"action": "delete", "name": "same-tree"}),
        ):
            result = await client.call_tool(tool, arguments)
            assert not result.is_error, (tool, result.content)
            assert result.structured_content["error"] is None
            assert result.structured_content["synchronization_error"] is None
            assert_retained_sessions(retained_analysis)
        empty = await client.call_tool("git_commit", {"message": "Nothing to commit"})
        assert not empty.is_error
        result = empty.structured_content
        assert result["head_before"] == result["head_after"]
        assert "nothing to commit" in result["error"]
        assert "git_status()" in result["error"] and "process_get(process_id=" in result["error"]
        assert result["synchronization_error"] is None
        assert_retained_sessions(retained_analysis)


@pytest.mark.anyio
async def test_network_metadata_mutations_retain_sessions(git_project, retained_analysis):
    """Local fetch and push leave glob-dependent contexts and their language servers intact."""
    remote = git_project.temporary / "remote.git"
    await git_project.command("clone", "--bare", str(git_project.root), str(remote))
    await git_project.command("remote", "add", "origin", str(remote))
    async with Client(retained_analysis.runtime.mcp) as client:
        for tool in ("git_fetch", "git_push"):
            result = await client.call_tool(tool, {})
            assert not result.is_error
            assert result.structured_content["error"] is None
            assert result.structured_content["synchronization_error"] is None
            assert_retained_sessions(retained_analysis)


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["create", "delete", "rename", "type", "content"])
async def test_switch_checks_physical_structure_and_retains_independent_sessions(
    git_project,
    retained_analysis,
    change,
):
    """Only real path/type changes withdraw structural contexts; content edits notify sessions."""
    await git_project.command("branch", "before-change")
    source = git_project.root / "src/math.cpp"
    if change == "create":
        (git_project.root / "added.cpp").write_text("int added;\n", encoding="utf-8")
    elif change == "delete":
        source.unlink()
    elif change == "rename":
        source.rename(source.with_name("renamed.cpp"))
    elif change == "type":
        source.unlink()
        source.mkdir()
        (source / "nested.cpp").write_text("int nested;\n", encoding="utf-8")
    else:
        source.write_bytes(source.read_bytes() + b"\n// changed content\n")
    await git_project.commit("Change source tree")
    await git_project.command("switch", "before-change")
    async with Client(retained_analysis.runtime.mcp) as client:
        result = await client.call_tool("git_switch", {"branch": "main"})
        assert not result.is_error
        assert result.structured_content["error"] is None
        assert result.structured_content["synchronization_error"] is None
        if change == "content":
            assert_retained_sessions(retained_analysis)
        else:
            assert set(retained_analysis.runtime.analysis.sessions) == {"release"}
            retained_analysis.peers["debug"].shutdown.assert_awaited_once()
        peer = retained_analysis.peers["release"]
        assert retained_analysis.runtime.analysis.sessions["release"].session is peer
        peer.shutdown.assert_not_awaited()
        peer.notify.assert_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("command", "structural"),
    [
        ("printf 'int hook;\\n' > src/hook.cpp", True),
        ("rm src/math.cpp", True),
        ("printf '\\n// hook edit\\n' >> src/math.cpp", False),
    ],
)
async def test_failed_commit_hook_still_checks_source_changes(
    git_project,
    retained_analysis,
    command,
    structural,
):
    """A failed commit can change working files without changing HEAD or losing provenance."""
    hook = git_project.root / ".git/hooks/pre-commit"
    hook.write_text(f"#!/bin/sh\n{command}\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    async with Client(retained_analysis.runtime.mcp) as client:
        response = await client.call_tool("git_commit", {"message": "Rejected by hook"})
        assert not response.is_error
        result = response.structured_content
        assert result["head_before"] == result["head_after"]
        assert result["error"] and result["process_ids"]
        assert "git_status()" in result["error"] and "process_get(process_id=" in result["error"]
        assert result["synchronization_error"] is None
        if structural:
            assert set(retained_analysis.runtime.analysis.sessions) == {"release"}
            retained_analysis.peers["debug"].shutdown.assert_awaited_once()
        else:
            assert_retained_sessions(retained_analysis)


@pytest.mark.anyio
async def test_commit_untracking_ignored_file_retains_sessions(git_project, retained_analysis):
    """Removing an ignored file from the index must not look like removing it from disk."""
    source = git_project.root / "ignored.cpp"
    source.write_text("int ignored;\n", encoding="utf-8")
    await git_project.command("add", "ignored.cpp")
    (git_project.root / ".gitignore").write_text("ignored.cpp\n", encoding="utf-8")
    await git_project.commit("Track an ignored file")
    readme = git_project.root / "README.md"
    readme.write_bytes(readme.read_bytes() + b"\nCommit with an index change\n")
    await git_project.command("add", "README.md")
    hook = git_project.root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\ngit rm --cached -- ignored.cpp\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    async with Client(retained_analysis.runtime.mcp) as client:
        response = await client.call_tool("git_commit", {"message": "Untrack existing file"})
        assert not response.is_error
        assert response.structured_content["error"] is None
        assert response.structured_content["synchronization_error"] is None
        assert source.is_file()
        assert_retained_sessions(retained_analysis)


@pytest.mark.anyio
async def test_source_snapshot_handles_empty_duplicate_missing_and_explicit_paths(
    git_project,
    monkeypatch,
):
    """Snapshots preserve literal names, collapse index duplicates, and retain unlisted paths."""
    runtime = git_project.runtime()
    source = WorkspacePath("project/unlisted α file.cpp")
    (git_project.root / source.relative).write_text("int source;\n", encoding="utf-8")
    missing = WorkspacePath("project/missing.cpp")
    catalog = AsyncMock(return_value=CommandResult(42, 0, b"", ""))
    monkeypatch.setattr(runtime.git, "command", catalog)
    assert await runtime.git.source_snapshot() == {}
    before = await runtime.git.source_snapshot([source, missing])
    assert before == {source: stat.S_IFREG, missing: None}
    catalog.return_value = CommandResult(
        43,
        0,
        f"{source.relative}\0{source.relative}\0".encode("utf-8"),
        "",
    )
    assert await runtime.git.source_snapshot(before) == before
    catalog.return_value = CommandResult(44, 0, b"", "")
    assert await runtime.git.source_snapshot(before) == before
    await runtime.close_services()


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["limit", "access"])
async def test_source_snapshot_failure_before_mutation_preserves_repository_and_sessions(
    git_project,
    retained_analysis,
    monkeypatch,
    failure,
):
    """Snapshot preconditions fail actionably without staging files or closing healthy sessions."""
    runtime = retained_analysis.runtime
    source = git_project.root / "src/math.cpp"
    source.write_bytes(source.read_bytes() + b"\n// unstaged edit\n")
    original_lstat = Path.lstat

    def checked_lstat(path, *args, **kwargs):
        """Deny just one source entry without leaking its native filename into errors."""
        if path == source:
            raise PermissionError(13, "Access denied", "PRIVATE_NATIVE_FILENAME")
        return original_lstat(path, *args, **kwargs)

    async with Client(runtime.mcp) as client:
        if failure == "limit":
            monkeypatch.setattr(runtime.git, "MAX_SOURCE_PATHS", 1)
        else:
            monkeypatch.setattr(Path, "lstat", checked_lstat)
        response = await client.call_tool("git_stage", {"paths": ["project/src/math.cpp"]})
        assert response.is_error
        message = response.content[0].text
        assert "git_status()" in message and "process_get(process_id=" in message
        if failure == "limit":
            assert "1-path limit" in message and ".gitignore" in message
        else:
            assert "project/src/math.cpp" in message and "permissions" in message
            assert "PRIVATE_NATIVE_FILENAME" not in message
        assert_retained_sessions(retained_analysis)
        assert not (await git_project.command("diff", "--cached", "--name-only")).strip()


@pytest.mark.anyio
async def test_failed_post_snapshot_preserves_completed_commit_and_invalidates_unknown_state(
    git_project,
    retained_analysis,
    monkeypatch,
):
    """An unavailable after-snapshot retains the commit and discards stale analysis."""
    source = git_project.root / "staged.cpp"
    source.write_text("int staged;\n", encoding="utf-8")
    await git_project.command("add", "staged.cpp")
    runtime = retained_analysis.runtime
    original = runtime.git.source_snapshot
    snapshots = 0

    async def checked_snapshot(paths=()):
        """Fail only after the real Git commit has completed."""
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            raise GitError("PRIVATE_SNAPSHOT_ERROR")
        return await original(paths)

    monkeypatch.setattr(runtime.git, "source_snapshot", checked_snapshot)
    async with Client(runtime.mcp) as client:
        response = await client.call_tool("git_commit", {"message": "Commit once"})
        assert not response.is_error
        result = response.structured_content
        assert result["error"] is None and result["head_before"] != result["head_after"]
        warning = result["synchronization_error"]
        assert "unknown" in warning and "Do not repeat the completed mutation" in warning
        assert "git_status()" in warning and "process_get(process_id=" in warning
        assert "PRIVATE_SNAPSHOT_ERROR" not in warning and result["process_ids"]
        assert not runtime.analysis.sessions and not runtime.analysis.contexts
        assert not runtime.analysis.lock.locked() and not runtime.builds.operation_lock.locked()
        assert all(not snapshot.current for snapshot in runtime.builds.inputs.values())
        assert (await git_project.command("rev-parse", "HEAD")).strip() == result["head_after"]


@pytest.mark.anyio
async def test_cancelled_post_snapshot_invalidates_contexts_and_releases_locks(
    git_project,
    retained_analysis,
    monkeypatch,
):
    """Cancellation after a completed commit cannot retain unverified source state or replay it."""
    source = git_project.root / "pending.cpp"
    source.write_text("int pending;\n", encoding="utf-8")
    await git_project.command("add", "pending.cpp")
    runtime = retained_analysis.runtime
    original = runtime.git.source_snapshot
    started = asyncio.Event()
    snapshots = 0

    async def checked_snapshot(paths=()):
        """Hold the after-snapshot until its owning SDK request is cancelled."""
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            started.set()
            await asyncio.Event().wait()
        return await original(paths)

    monkeypatch.setattr(runtime.git, "source_snapshot", checked_snapshot)
    async with Client(runtime.mcp) as client:
        task = asyncio.create_task(client.call_tool("git_commit", {"message": "Commit once"}))
        try:
            await asyncio.wait_for(started.wait(), 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            status = await asyncio.wait_for(client.call_tool("git_status", {}), 5)
            assert not status.is_error
            assert not runtime.analysis.sessions
            assert not runtime.analysis.lock.locked() and not runtime.builds.operation_lock.locked()
            assert not runtime.workspace.operation_lock.locked()
            assert all(not snapshot.current for snapshot in runtime.builds.inputs.values())
            assert (await git_project.command("rev-list", "--count", "HEAD")).strip() == "2"
        finally:
            if not task.done():
                task.cancel()


@pytest.mark.anyio
async def test_source_kind_does_not_follow_leaf_links_or_external_parents(git_project):
    """Inspect link metadata and reject parents that would direct reads outside project/."""
    link = git_project.root / "linked"
    try:
        link.symlink_to(git_project.temporary, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symbolic links requires local OS support or privileges.")
    runtime = git_project.runtime()
    assert runtime.git.source_kind(WorkspacePath("project/linked")) == stat.S_IFLNK
    with pytest.raises(GitError) as caught:
        runtime.git.source_kind(WorkspacePath("project/linked/outside.cpp"))
    message = str(caught.value)
    assert "project/linked/outside.cpp" in message and "outside project/" in message
    assert "git_status()" in message and "operator" in message
    await runtime.close_services()


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
        assert "Do not repeat the completed mutation" in result["synchronization_error"]
        assert "private diagnostic detail" not in str(result)


@pytest.mark.anyio
async def test_stdout_only_commit_failure_retains_a_useful_typed_outcome(git_project):
    """Explain a nothing-to-commit exit while preserving the mutation result and unchanged HEAD."""
    async with Client(git_project.runtime().mcp) as client:
        response = await client.call_tool("git_commit", {"message": "No changes"})
        assert not response.is_error
        result = response.structured_content
        assert result["head_before"] == result["head_after"]
        assert "nothing to commit" in result["error"]
        assert "stdout" in result["error"] and "process_get(process_id=" in result["error"]
        assert "git_status()" in result["error"] and result["process_ids"]


@pytest.mark.anyio
async def test_parser_failure_points_to_original_process_without_returning_raw_data(
    git_project,
    monkeypatch,
):
    """Keep machine-output errors actionable without exposing the malformed record itself."""
    def fail_status(_data):
        """Model an incompatible porcelain record while keeping its contents private."""
        raise GitParseError("Cannot parse Git porcelain v2 status.")

    monkeypatch.setattr(parsers, "status", fail_status)
    async with Client(git_project.runtime().mcp) as client:
        response = await client.call_tool("git_status", {})
        assert response.is_error
        message = response.content[0].text
        assert "porcelain v2 status" in message
        assert "process_get(process_id=" in message and "compatibility" in message
