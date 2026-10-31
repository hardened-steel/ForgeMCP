"""Git tool boundaries reject alternate repositories and option/refspec injection."""

from mcp import Client
import pytest

from forgemcp.git.errors import GitError
from forgemcp.git.service import failure_reason, literal_identifier
from forgemcp.toolchain.tools.git import CommandResult


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("", "must not be empty"),
        ("--all", "must not begin"),
        ("bad\0ref", "NUL"),
        ("bad\nref", "newline"),
    ],
)
def test_identifier_errors_name_the_argument_and_escape_its_value(value, reason):
    """Identify the failing list element without allowing control characters into its message."""
    with pytest.raises(GitError) as caught:
        literal_identifier(value, "revisions[2]")
    message = str(caught.value)
    assert "revisions[2]=" in message and reason in message
    assert "\0" not in message and "\n" not in message


def test_failed_exit_uses_bounded_redacted_stdout_when_stderr_is_empty():
    """Preserve a stdout-only Git failure while hiding URL credentials and oversized output."""
    result = CommandResult(
        42,
        1,
        b"nothing to commit: https://user:private@example.invalid/repo\n" + b"x" * 10000,
        "",
    )
    message = failure_reason(result)
    assert "stdout" in message and "nothing to commit" in message
    assert "[redacted]@example.invalid" in message
    assert "user:private" not in message
    assert "process_get(process_id=42)" in message and len(message) < 500


def test_failed_exit_redacts_credentials_crossing_the_stdout_excerpt_boundary():
    """Do not reveal a URL password when clipping removes its terminating at-sign."""
    result = CommandResult(
        42,
        1,
        b"fatal: https://user:" + b"PRIVATE_CREDENTIAL" * 400 + b"@example.invalid/repo",
        "",
    )
    message = failure_reason(result)
    assert "https://[redacted]" in message
    assert "PRIVATE_CREDENTIAL" not in message and "user:" not in message
    assert "process_get(process_id=42)" in message


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        (
            "git_diff",
            {"mode": "revision", "base": "HEAD", "target": "missing-commit"},
            "target='missing-commit'",
        ),
        (
            "git_restore",
            {"paths": ["project/CMakeLists.txt"], "source": "missing-commit"},
            "source='missing-commit'",
        ),
        (
            "git_branch",
            {"action": "create", "name": "topic", "start_point": "missing-commit"},
            "start_point='missing-commit'",
        ),
        (
            "git_cherry_pick",
            {"revisions": ["HEAD", "missing-commit"]},
            "revisions[1]='missing-commit'",
        ),
    ],
)
async def test_revision_errors_preserve_callsite_parameter_labels(
    git_project,
    tool,
    arguments,
    expected,
):
    """Point each revision-based tool at its actual bad parameter before any mutation runs."""
    before = await git_project.command("rev-parse", "HEAD")
    async with Client(git_project.runtime().mcp) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    message = result.content[0].text
    assert expected in message
    assert "git_log()" in message and "process_get(process_id=" in message
    assert await git_project.command("rev-parse", "HEAD") == before


@pytest.mark.anyio
async def test_argument_errors_include_current_values_counts_and_next_calls(git_project):
    """Return concrete corrections for paths, stashes, ranges, and incompatible arguments."""
    cases = [
        ("git_stage", {"paths": []}, ("paths contains 0", "1 and 1000", "git_status()")),
        (
            "git_stage",
            {"paths": ["project/CMakeLists.txt", "storage/invalid.cpp"]},
            ("paths[1]='storage/invalid.cpp'", "project/", "git_status()"),
        ),
        (
            "git_diff",
            {"mode": "staged", "base": "HEAD"},
            ("mode='staged'", "base='HEAD'", "Remove base/target"),
        ),
        (
            "git_blame",
            {"path": "project/CMakeLists.txt", "start_line": 8, "end_line": 3},
            ("end_line=3", "start_line=8", "inclusive"),
        ),
        (
            "git_stash",
            {"action": "apply", "reference": "HEAD"},
            ("reference='HEAD'", "stash@{N}", "git_stash(action='list')"),
        ),
        (
            "git_pull",
            {"branch": "main"},
            ("branch='main'", "remote=null", "git_remotes()"),
        ),
        (
            "git_continue",
            {"action": "continue", "operation": "merge"},
            ("no unfinished Git operation", "action='continue'", "git_status()"),
        ),
        (
            "git_revert",
            {"revisions": []},
            ("revisions contains 0", "1 and 100", "git_log()"),
        ),
    ]
    async with Client(git_project.runtime().mcp) as client:
        for tool, arguments, expected in cases:
            result = await client.call_tool(tool, arguments)
            assert result.is_error, (tool, result.content)
            assert all(part in result.content[0].text for part in expected), result.content


@pytest.mark.anyio
async def test_unknown_remote_lists_choices_and_preserves_credential_redaction(git_project):
    """List configured remote names without making a network call or returning credentials."""
    await git_project.command("remote", "add", "origin", "https://example.invalid/repo")
    async with Client(git_project.runtime().mcp) as client:
        result = await client.call_tool(
            "git_fetch",
            {"remote": "https://user:private@example.invalid/repo"},
        )
    assert result.is_error
    message = result.content[0].text
    assert "remote=" in message and "'origin'" in message and "git_remotes()" in message
    assert "user:private" not in message


@pytest.mark.anyio
async def test_unknown_remote_explains_empty_and_large_catalogs(git_project):
    """Handle zero and many remote choices without suggesting a nonexistent default remote."""
    async with Client(git_project.runtime().mcp) as client:
        empty = await client.call_tool("git_fetch", {})
        assert empty.is_error
        assert "No values" in empty.content[0].text and "operator" in empty.content[0].text
        for index in range(9):
            await git_project.command(
                "remote",
                "add",
                f"remote-{index}",
                "https://example.invalid/repo",
            )
        many = await client.call_tool("git_fetch", {})
        assert many.is_error
        assert "9 available values" in many.content[0].text
        assert "git_remotes()" in many.content[0].text and "remote-8" not in many.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "storage/outside.cpp",
        "root/C:/outside.cpp",
        "project/.git/config",
        "project/",
    ],
)
async def test_stage_rejects_non_source_roots_and_git_metadata(git_project, path):
    """Reject paths outside explicit project source selections before mutating the index."""
    async with Client(git_project.runtime().mcp) as client:
        result = await client.call_tool("git_stage", {"paths": [path]})
        assert result.is_error
    assert not (await git_project.command("diff", "--cached", "--name-only")).strip()


@pytest.mark.anyio
async def test_literal_pathspec_does_not_stage_matching_neighbour(git_project):
    """A file containing glob syntax stages only itself through the literal argument boundary."""
    literal = git_project.root / "wild[1].cpp"
    neighbour = git_project.root / "wild1.cpp"
    literal.write_text("literal\n", encoding="utf-8")
    neighbour.write_text("neighbour\n", encoding="utf-8")
    async with Client(git_project.runtime().mcp) as client:
        result = await client.call_tool("git_stage", {"paths": ["project/wild[1].cpp"]})
        assert not result.is_error
        assert result.structured_content["error"] is None
    assert (await git_project.command("diff", "--cached", "--name-only")).strip() == "wild[1].cpp"


@pytest.mark.anyio
async def test_workspace_inside_ancestor_repository_cannot_mutate_parent(git_project):
    """An instance rooted in a repository subfolder must not operate on its ancestor repository."""
    async with Client(git_project.runtime(git_project.root / "src").mcp) as client:
        result = await client.call_tool("git_status", {})
        assert result.is_error
        assert "worktree root" in result.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize("branch", ["main:refs/heads/victim", "refs/heads/*:refs/heads/*"])
async def test_pull_rejects_fetch_refspec_as_branch(git_project, branch):
    """An explicit pull branch must not introduce destination refs or wildcard fetches."""
    remote = git_project.temporary / "remote.git"
    await git_project.command("clone", "--bare", str(git_project.root), str(remote))
    await git_project.command("remote", "add", "origin", str(remote))
    async with Client(git_project.runtime().mcp) as client:
        result = await client.call_tool("git_pull", {"remote": "origin", "branch": branch})
        assert result.is_error
    assert not (await git_project.command("branch", "--list", "victim")).strip()


@pytest.mark.anyio
async def test_revision_parameters_are_rejected_for_staged_diff(git_project):
    """Do not silently substitute a different comparison when the caller specifies revisions."""
    async with Client(git_project.runtime().mcp) as client:
        result = await client.call_tool("git_diff", {"mode": "staged", "base": "HEAD"})
        assert result.is_error


@pytest.mark.anyio
async def test_revision_option_cannot_become_git_argument(git_project):
    """Reject option-like revisions instead of allowing them into revision resolution."""
    async with Client(git_project.runtime().mcp) as client:
        result = await client.call_tool("git_show", {"revision": "--all"})
        assert result.is_error


@pytest.mark.anyio
async def test_inherited_repository_environment_cannot_redirect_status(git_project, monkeypatch):
    """Repository selection remains the configured worktree despite inherited Git redirection."""
    monkeypatch.setenv("GIT_DIR", str(git_project.temporary / "missing.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(git_project.temporary))
    async with Client(git_project.runtime().mcp) as client:
        result = await client.call_tool("git_status", {})
        assert not result.is_error
        assert result.structured_content["branch"] == "main"


@pytest.mark.anyio
async def test_status_recovers_rebase_stopped_by_exec_in_new_runtime(git_project):
    """Discover a stopped rebase even when no conflicted commit pseudoref is present."""
    (git_project.root / "extra.cpp").write_text("int extra;\n", encoding="utf-8")
    await git_project.commit("Add second commit")
    await git_project.command("rebase", "--exec", "false", "HEAD~1", success=False)
    assert (git_project.root / ".git" / "rebase-merge").is_dir()
    async with Client(git_project.runtime().mcp) as client:
        result = await client.call_tool("git_status", {})
        assert not result.is_error
        assert result.structured_content["operation"] == "rebase"
