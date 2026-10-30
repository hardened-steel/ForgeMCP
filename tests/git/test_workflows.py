"""Public Git workflows against disposable repositories and local file remotes."""

import asyncio
from typing import Any

from mcp import Client
import pytest


async def call(
    client: Client,
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    success: bool = True,
) -> dict[str, Any]:
    """Require a typed public response and optionally a successful domain outcome."""
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error, result.content
    assert result.content and result.content[0].text
    data = result.structured_content
    assert isinstance(data, dict)
    if success:
        assert data.get("error") is None, data
    return data


@pytest.mark.anyio
async def test_git_tools_expose_typed_text_contract_and_progress(git_project):
    """Keep typed backend results, icons, progress, and history available to both clients."""
    runtime = git_project.runtime()
    notifications = []

    async def progress(value, total, message):
        """Capture the public monotonic progress sequence for one read operation."""
        notifications.append((value, total, message))

    async with Client(runtime.mcp) as client:
        tools = {
            tool.name: tool
            for tool in (await client.list_tools()).tools
            if tool.name.startswith("git_")
        }
        expected = {
            "git_status",
            "git_stage",
            "git_unstage",
            "git_commit",
            "git_restore",
            "git_branch",
            "git_switch",
            "git_stash",
            "git_fetch",
            "git_pull",
            "git_push",
            "git_merge",
            "git_rebase",
            "git_cherry_pick",
            "git_revert",
            "git_continue",
            "git_conflicts",
            "git_branches",
            "git_tags",
            "git_remotes",
            "git_log",
            "git_show",
            "git_diff",
            "git_blame",
        }
        assert expected <= tools.keys()
        assert "git_run" not in tools and "git_worktree" not in tools
        for tool in tools.values():
            assert tool.output_schema and tool.icons
            assert "ctx" not in tool.input_schema.get("properties", {})
        response = await client.call_tool("git_status", {}, progress_callback=progress)
        assert not response.is_error and response.structured_content
        assert response.content and response.content[0].text
        assert notifications
        values = [item[0] for item in notifications]
        assert values == sorted(values)
    assert any(item.tool_name == "git_status" for item in runtime.history.list_calls())


@pytest.mark.anyio
async def test_status_stage_commit_unstage_restore_and_deleted_paths(git_project):
    """Preserve Unicode names, index/worktree distinctions, literal paths, and deleted files."""
    filename = "src/space α.cpp"
    target = git_project.root / filename
    target.write_text("int value = 1;\n", encoding="utf-8")
    runtime = git_project.runtime()
    async with Client(runtime.mcp) as client:
        status = await call(client, "git_status")
        assert status["branch"] == "main"
        assert any(entry["path"] == f"project/{filename}" for entry in status["entries"])
        staged = await call(client, "git_stage", {"paths": [f"project/{filename}"]})
        assert staged["process_ids"]
        status = await call(client, "git_status")
        entry = next(item for item in status["entries"] if item["path"] == f"project/{filename}")
        assert entry["index_status"] == "A"
        await call(client, "git_commit", {"message": "Add Unicode source"})
        assert (await call(client, "git_status"))["entries"] == []
        target.write_text("int value = 2;\n", encoding="utf-8")
        await call(client, "git_stage", {"paths": [f"project/{filename}"]})
        await call(client, "git_unstage", {"paths": [f"project/{filename}"]})
        await call(client, "git_restore", {"paths": [f"project/{filename}"]})
        assert target.read_text(encoding="utf-8") == "int value = 1;\n"
        target.unlink()
        await call(client, "git_stage", {"paths": [f"project/{filename}"]})
        status = await call(client, "git_status")
        assert any(entry["index_status"] == "D" for entry in status["entries"])
        await call(client, "git_commit", {"message": "Remove Unicode source"})
        assert (await call(client, "git_status"))["entries"] == []


@pytest.mark.anyio
async def test_diff_history_show_blame_refs_and_redacted_remotes(git_project):
    """Read native patches and committed attribution without exposing credentials in results."""
    target = git_project.root / "history.cpp"
    target.write_bytes(b"int first;\nint second;\n")
    first = await git_project.commit("First history snapshot")
    target.write_bytes(b"int first;\nint modified;\n")
    second = await git_project.commit("Second history snapshot")
    await git_project.command("tag", "v1", first)
    await git_project.command(
        "remote",
        "add",
        "example",
        "https://test-user:test-secret@example.invalid/repository.git",
    )
    target.write_text("int first;\nint unstaged;\n", encoding="utf-8")
    async with Client(git_project.runtime().mcp) as client:
        diff = await call(client, "git_diff", {"paths": ["project/history.cpp"]})
        assert diff["files"][0]["path"] == "project/history.cpp"
        assert "+int unstaged;" in diff["patch"]
        await call(client, "git_stage", {"paths": ["project/history.cpp"]})
        staged = await call(client, "git_diff", {"mode": "staged"})
        assert "+int unstaged;" in staged["patch"]
        revisions = await call(
            client,
            "git_diff",
            {"mode": "revision", "base": first, "target": second},
        )
        assert "+int modified;" in revisions["patch"]
        history = await call(client, "git_log", {"limit": 1})
        assert history["revision"] == second
        assert history["commits"][0]["oid"] == second
        assert history["next_skip"] == 1
        earlier = await call(
            client,
            "git_log",
            {"revision": history["revision"], "limit": 1, "skip": 1},
        )
        assert earlier["commits"][0]["oid"] == first
        details = await call(client, "git_show", {"revision": second})
        assert details["commit"]["oid"] == second
        assert "+int modified;" in details["diff"]["patch"]
        attributed = await call(
            client,
            "git_blame",
            {"path": "project/history.cpp", "revision": second, "start_line": 2, "end_line": 2},
        )
        assert attributed["lines"][0]["oid"] == second
        assert attributed["lines"][0]["line"] == 2
        assert attributed["lines"][0]["text"] == "int modified;"
        branches = await call(client, "git_branches")
        assert any(ref["name"] == "refs/heads/main" and ref["current"] for ref in branches["refs"])
        tags = await call(client, "git_tags")
        assert any(ref["name"] == "refs/tags/v1" and ref["oid"] == first for ref in tags["refs"])
        remotes = await call(client, "git_remotes")
        assert remotes["remotes"][0]["name"] == "example"
        assert "test-secret" not in str(remotes)
        assert "test-user" not in str(remotes)


@pytest.mark.anyio
async def test_renamed_and_binary_files_keep_git_native_diff_metadata(git_project):
    """Preserve rename source paths and report binary changes without decoding blob contents."""
    source = git_project.root / "before.cpp"
    source.write_text("int renamed;\n", encoding="utf-8")
    binary = git_project.root / "image.bin"
    binary.write_bytes(b"\0old\xff")
    await git_project.commit("Rename and binary base")
    await git_project.command("mv", "before.cpp", "after.cpp")
    binary.write_bytes(b"\0new\xfe")
    async with Client(git_project.runtime().mcp) as client:
        status = await call(client, "git_status")
        rename = next(entry for entry in status["entries"] if entry["path"] == "project/after.cpp")
        assert rename["original_path"] == "project/before.cpp"
        staged = await call(client, "git_diff", {"mode": "staged"})
        rename = next(entry for entry in staged["files"] if entry["path"] == "project/after.cpp")
        assert rename["original_path"] == "project/before.cpp"
        assert rename["status"].startswith("R")
        changed = await call(client, "git_diff", {"paths": ["project/image.bin"]})
        assert changed["files"][0]["path"] == "project/image.bin"
        assert "Binary files" in changed["patch"]


@pytest.mark.anyio
async def test_unborn_branch_can_stage_unstage_and_record_initial_commit(git_project):
    """Handle an unborn HEAD without assuming that HEAD resolves to a commit."""
    await git_project.command("switch", "--orphan", "empty")
    target = git_project.root / "first.cpp"
    target.write_text("int first;\n", encoding="utf-8")
    async with Client(git_project.runtime().mcp) as client:
        status = await call(client, "git_status")
        assert status["head"] is None
        assert status["branch"] == "empty"
        await call(client, "git_stage", {"paths": ["project/first.cpp"]})
        await call(client, "git_unstage", {"paths": ["project/first.cpp"]})
        status = await call(client, "git_status")
        assert status["head"] is None
        assert any(entry["path"] == "project/first.cpp" for entry in status["entries"])
        await call(client, "git_stage", {"paths": ["project/first.cpp"]})
        committed = await call(client, "git_commit", {"message": "First commit"})
        assert committed["head_before"] is None
        assert committed["head_after"]


@pytest.mark.anyio
async def test_branch_and_stash_lifecycle(git_project):
    """Create and remove a branch while exercising stash save, apply, pop, and drop."""
    target = git_project.root / "README.md"
    original = target.read_text(encoding="utf-8")
    async with Client(git_project.runtime().mcp) as client:
        await call(client, "git_branch", {"action": "create", "name": "topic"})
        await call(client, "git_switch", {"branch": "topic"})
        assert (await call(client, "git_status"))["branch"] == "topic"
        target.write_text(original + "\nStashed change\n", encoding="utf-8")
        await call(client, "git_stash", {"action": "save", "message": "Pending change"})
        assert target.read_text(encoding="utf-8") == original
        await call(client, "git_stash", {"action": "list"})
        await call(client, "git_stash", {"action": "apply"})
        assert "Stashed change" in target.read_text(encoding="utf-8")
        await call(client, "git_restore", {"paths": ["project/README.md"]})
        await call(client, "git_stash", {"action": "pop"})
        assert "Stashed change" in target.read_text(encoding="utf-8")
        assert not (await git_project.command("stash", "list")).strip()
        await call(client, "git_stash", {"action": "save"})
        await call(client, "git_stash", {"action": "drop"})
        assert not (await git_project.command("stash", "list")).strip()
        await call(client, "git_switch", {"branch": "main"})
        await call(client, "git_branch", {"action": "delete", "name": "topic"})


@pytest.mark.anyio
async def test_merge_conflict_sides_resolution_and_continue(git_project):
    """Expose immutable conflict sides and continue a merge after explicit resolution."""
    target = git_project.root / "conflict.cpp"
    target.write_bytes(b"base\n")
    await git_project.commit("Common ancestor")
    await git_project.command("switch", "--create", "topic")
    target.write_bytes(b"topic\n")
    await git_project.commit("Topic change")
    await git_project.command("switch", "main")
    target.write_bytes(b"main\n")
    await git_project.commit("Main change")
    async with Client(git_project.runtime().mcp) as client:
        merged = await call(client, "git_merge", {"revision": "topic"}, success=False)
        assert merged["error"]
        assert merged["operation"] == "merge"
        assert "project/conflict.cpp" in merged["conflicts"]
        conflict = (await call(client, "git_conflicts"))["files"][0]
        assert conflict["path"] == "project/conflict.cpp"
        assert conflict["base"]["text"] == "base\n"
        assert conflict["ours"]["text"] == "main\n"
        assert conflict["theirs"]["text"] == "topic\n"
        await call(
            client,
            "workspace_write_file",
            {"path": "project/conflict.cpp", "text": "resolved\n"},
        )
        await call(client, "git_stage", {"paths": ["project/conflict.cpp"]})
        await call(client, "git_continue", {"action": "continue"})
        status = await call(client, "git_status")
        assert status["operation"] is None
        assert status["entries"] == []
        parents = await git_project.command("rev-list", "--parents", "-n", "1", "HEAD")
        assert len(parents.split()) == 3


@pytest.mark.anyio
async def test_merge_abort_restores_head_and_worktree(git_project):
    """Abort a conflicting merge without losing the previous branch tip or contents."""
    target = git_project.root / "conflict.txt"
    target.write_text("base\n", encoding="utf-8")
    await git_project.commit("Common ancestor")
    await git_project.command("switch", "--create", "other")
    target.write_text("other\n", encoding="utf-8")
    await git_project.commit("Other change")
    await git_project.command("switch", "main")
    target.write_text("main\n", encoding="utf-8")
    before = await git_project.commit("Main change")
    async with Client(git_project.runtime().mcp) as client:
        merged = await call(client, "git_merge", {"revision": "other"}, success=False)
        assert merged["error"]
        await call(client, "git_continue", {"action": "abort"})
        status = await call(client, "git_status")
        assert status["head"] == before
        assert status["operation"] is None
        assert target.read_text(encoding="utf-8") == "main\n"


@pytest.mark.anyio
async def test_cherry_pick_revert_and_nonconflicting_rebase(git_project):
    """Apply and revert a commit, then rebase an independent topic commit onto main."""
    await git_project.command("switch", "--create", "donor")
    (git_project.root / "donor.cpp").write_text("int donor;\n", encoding="utf-8")
    donor = await git_project.commit("Donor change")
    await git_project.command("switch", "main")
    async with Client(git_project.runtime().mcp) as client:
        picked = await call(client, "git_cherry_pick", {"revisions": [donor]})
        assert (git_project.root / "donor.cpp").is_file()
        await call(client, "git_revert", {"revisions": [picked["head_after"]]})
        assert not (git_project.root / "donor.cpp").exists()
        await call(client, "git_switch", {"branch": "topic", "create": True})
        (git_project.root / "topic.cpp").write_text("int topic;\n", encoding="utf-8")
        await call(client, "git_stage", {"paths": ["project/topic.cpp"]})
        await call(client, "git_commit", {"message": "Topic change"})
        await call(client, "git_switch", {"branch": "main"})
        (git_project.root / "main.cpp").write_text("int main_value;\n", encoding="utf-8")
        await call(client, "git_stage", {"paths": ["project/main.cpp"]})
        await call(client, "git_commit", {"message": "Main change"})
        await call(client, "git_switch", {"branch": "topic"})
        await call(client, "git_rebase", {"revision": "main"})
        assert (git_project.root / "main.cpp").is_file()
        assert (git_project.root / "topic.cpp").is_file()
        assert not (await call(client, "git_status"))["operation"]


@pytest.mark.anyio
async def test_local_remote_fetch_pull_push_and_ff_only_divergence(git_project):
    """Use a local bare remote and reject divergent pull without rewriting local history."""
    remote = git_project.temporary / "remote.git"
    remote.mkdir()
    await git_project.command("init", "--bare", "--initial-branch=main", cwd=remote)
    await git_project.command("remote", "add", "origin", str(remote))
    async with Client(git_project.runtime().mcp) as client:
        await call(client, "git_push", {"set_upstream": True})
        await call(client, "git_fetch")
        await call(client, "git_pull")
        await git_project.command("switch", "--create", "remote-advance")
        (git_project.root / "remote.cpp").write_text("int remote;\n", encoding="utf-8")
        await git_project.commit("Remote advancement")
        await git_project.command("push", "origin", "HEAD:main")
        await git_project.command("switch", "main")
        (git_project.root / "local.cpp").write_text("int local;\n", encoding="utf-8")
        before = await git_project.commit("Local divergence")
        pulled = await call(client, "git_pull", success=False)
        assert pulled["error"]
        assert (await call(client, "git_status"))["head"] == before
        assert not (git_project.root / "remote.cpp").exists()
        await call(client, "git_pull", {"strategy": "merge"})
        assert (git_project.root / "remote.cpp").is_file()
        assert (git_project.root / "local.cpp").is_file()
        await call(client, "git_push")


@pytest.mark.anyio
async def test_push_lease_rejects_wrong_remote_tip(git_project):
    """Require the exact expected remote ref before allowing a lease-protected push."""
    remote = git_project.temporary / "remote.git"
    remote.mkdir()
    await git_project.command("init", "--bare", "--initial-branch=main", cwd=remote)
    await git_project.command("remote", "add", "origin", str(remote))
    await git_project.command("push", "--set-upstream", "origin", "main")
    initial = (await git_project.command("rev-parse", "HEAD")).strip()
    (git_project.root / "next.cpp").write_text("int next_value;\n", encoding="utf-8")
    next_commit = await git_project.commit("Next change")
    async with Client(git_project.runtime().mcp) as client:
        rejected = await call(
            client,
            "git_push",
            {"branch": "main", "expected_remote_oid": "0" * len(initial)},
            success=False,
        )
        assert rejected["error"]
        assert (await git_project.command("rev-parse", "main", cwd=remote)).strip() == initial
        await call(
            client,
            "git_push",
            {"branch": "main", "expected_remote_oid": initial},
        )
        assert (await git_project.command("rev-parse", "main", cwd=remote)).strip() == next_commit


@pytest.mark.anyio
async def test_conflicting_rebase_abort_keeps_original_topic_history(git_project):
    """Recognize an interrupted rebase and restore its original branch on abort."""
    target = git_project.root / "rebase.txt"
    target.write_text("base\n", encoding="utf-8")
    await git_project.commit("Rebase ancestor")
    await git_project.command("switch", "--create", "topic")
    target.write_text("topic\n", encoding="utf-8")
    topic = await git_project.commit("Topic line")
    await git_project.command("switch", "main")
    target.write_text("main\n", encoding="utf-8")
    await git_project.commit("Main line")
    await git_project.command("switch", "topic")
    async with Client(git_project.runtime().mcp) as client:
        rebased = await call(client, "git_rebase", {"revision": "main"}, success=False)
        assert rebased["error"]
        assert rebased["operation"] == "rebase"
        assert rebased["conflicts"] == ["project/rebase.txt"]
        await call(client, "git_continue", {"action": "abort"})
        status = await call(client, "git_status")
        assert status["branch"] == "topic"
        assert status["head"] == topic
        assert status["operation"] is None
        assert target.read_text(encoding="utf-8") == "topic\n"


@pytest.mark.anyio
async def test_linked_worktree_uses_external_repository_metadata(git_project):
    """Commit from a linked workspace without granting arbitrary external filesystem tools."""
    linked = git_project.temporary / "linked"
    await git_project.command("worktree", "add", "-b", "linked-topic", str(linked))
    target = linked / "linked.cpp"
    target.write_text("int linked;\n", encoding="utf-8")
    async with Client(git_project.runtime(linked).mcp) as client:
        assert (await call(client, "git_status"))["branch"] == "linked-topic"
        await call(client, "git_stage", {"paths": ["project/linked.cpp"]})
        committed = await call(client, "git_commit", {"message": "Linked change"})
        assert committed["head_after"]
        assert (await call(client, "git_status"))["entries"] == []
        outside = "root/" + git_project.root.as_posix() + "/unexpected.txt"
        rejected = await client.call_tool("workspace_write_file", {"path": outside, "text": "no"})
        assert rejected.is_error
    assert not (git_project.root / "unexpected.txt").exists()


@pytest.mark.anyio
async def test_commit_preserves_normal_hooks(git_project):
    """Keep the installed Git's commit hooks enabled for explicitly requested commits."""
    marker = git_project.root / ".git/hooks/commit-was-run"
    hook = git_project.root / ".git/hooks/post-commit"
    hook.write_text("#!/bin/sh\nprintf done > .git/hooks/commit-was-run\n", encoding="utf-8")
    hook.chmod(0o755)
    (git_project.root / "hook.cpp").write_text("int hook;\n", encoding="utf-8")
    async with Client(git_project.runtime().mcp) as client:
        await call(client, "git_stage", {"paths": ["project/hook.cpp"]})
        await call(client, "git_commit", {"message": "Run post-commit hook"})
    assert marker.read_text(encoding="utf-8") == "done"


@pytest.mark.anyio
async def test_cancelled_commit_releases_feature_locks_without_repeating_commit(git_project):
    """Cancel a sleeping pre-commit hook and permit a later explicit commit exactly once."""
    hook = git_project.root / ".git/hooks/pre-commit"
    started = git_project.root / ".git/hooks/pre-commit-started"
    hook.write_bytes(
        b"#!/bin/sh\nprintf started > .git/hooks/pre-commit-started\nsleep 30\n",
    )
    hook.chmod(0o755)
    (git_project.root / "pending.cpp").write_bytes(b"int pending;\n")
    initial = (await git_project.command("rev-parse", "HEAD")).strip()
    runtime = git_project.runtime()
    async with Client(runtime.mcp) as client:
        await call(client, "git_stage", {"paths": ["project/pending.cpp"]})
        task = asyncio.create_task(client.call_tool("git_commit", {"message": "Cancelled commit"}))
        try:
            async with asyncio.timeout(10):
                while not started.exists():
                    await asyncio.sleep(0.02)
            task.cancel()
            completed, pending = await asyncio.wait({task}, timeout=5)
            assert task in completed and not pending, "Cancelled Git call did not settle promptly."
            with pytest.raises(asyncio.CancelledError):
                task.result()
            async with asyncio.timeout(5):
                status = await call(client, "git_status")
            assert status["head"] == initial
            assert not runtime.workspace.operation_lock.locked()
            assert not runtime.builds.operation_lock.locked()
            assert not runtime.analysis.lock.locked()
            hook.unlink()
            await call(client, "git_commit", {"message": "Explicit later commit"})
            history = await call(client, "git_log", {"limit": 3})
            assert len(history["commits"]) == 2
            assert history["commits"][0]["message"].strip() == "Explicit later commit"
        finally:
            if not task.done():
                task.cancel()
