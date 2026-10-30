"""Git tool boundaries reject alternate repositories and option/refspec injection."""

from mcp import Client
import pytest


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
