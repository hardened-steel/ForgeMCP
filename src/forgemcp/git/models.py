"""Typed repository snapshots shared by operations, parsers, and text presentation."""

from typing import Literal

from pydantic import BaseModel, Field

from forgemcp.workspace.path import WorkspacePath

type GitOperation = Literal["merge", "rebase", "cherry_pick", "revert"]


class GitEntry(BaseModel):
    """Independent index and worktree states for one repository path."""

    path: WorkspacePath
    original_path: WorkspacePath | None = None
    index_status: str
    worktree_status: str
    submodule: str | None = None


class GitStatus(BaseModel):
    """One bounded repository status, including an unfinished integration operation."""

    branch: str | None = None
    head: str | None = None
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None
    entries: list[GitEntry] = Field(default_factory=list)
    operation: GitOperation | None = None
    process_ids: list[int] = Field(default_factory=list)


class GitChange(BaseModel):
    """Git change metadata preserving renames, object IDs, and file modes."""

    path: WorkspacePath
    original_path: WorkspacePath | None = None
    status: str
    old_mode: str
    new_mode: str
    old_oid: str
    new_oid: str


class GitDiff(BaseModel):
    """A Git-native patch and typed file metadata for an explicit comparison."""

    mode: Literal["unstaged", "staged", "revision"]
    base: str | None = None
    target: str | None = None
    files: list[GitChange]
    patch: str
    had_decoding_errors: bool = False
    process_ids: list[int] = Field(default_factory=list)


class GitCommit(BaseModel):
    """A complete commit identity, parents, authorship, and original message."""

    oid: str
    parents: list[str]
    author_name: str
    author_email: str
    author_time: str
    committer_name: str
    committer_email: str
    committer_time: str
    message: str


class GitHistory(BaseModel):
    """One bounded page of commits anchored to a resolved revision."""

    revision: str | None
    commits: list[GitCommit]
    next_skip: int | None = None
    process_ids: list[int] = Field(default_factory=list)


class GitCommitDetails(BaseModel):
    """A selected commit and its change against a chosen parent or the empty tree."""

    commit: GitCommit
    diff: GitDiff


class GitBlameLine(BaseModel):
    """Original and current line numbers with their attributed commit and author."""

    oid: str
    original_line: int
    line: int
    author: str
    author_email: str
    summary: str
    text: str


class GitBlame(BaseModel):
    """Line attribution in a selected committed file revision."""

    path: WorkspacePath
    revision: str
    lines: list[GitBlameLine]
    process_ids: list[int] = Field(default_factory=list)


class GitRef(BaseModel):
    """One branch or tag, retaining its full ref name and object identity."""

    name: str
    oid: str
    upstream: str | None = None
    current: bool = False


class GitRefs(BaseModel):
    """Local/remote branch or tag refs from one invocation."""

    refs: list[GitRef]
    process_ids: list[int] = Field(default_factory=list)


class GitRemote(BaseModel):
    """A configured remote with credential-redacted fetch and push addresses."""

    name: str
    fetch_urls: list[str]
    push_urls: list[str]


class GitRemotes(BaseModel):
    """Configured remotes without fetching network state."""

    remotes: list[GitRemote]
    process_ids: list[int] = Field(default_factory=list)


class GitStash(BaseModel):
    """One retained stash identified by its reflog selector and commit."""

    reference: str
    oid: str
    message: str


class GitStashes(BaseModel):
    """The bounded stash list of the current repository."""

    stashes: list[GitStash]
    process_ids: list[int] = Field(default_factory=list)


class GitBlob(BaseModel):
    """A conflict stage with its exact object identity and optional UTF-8 text."""

    oid: str
    mode: str
    binary: bool
    text: str | None


class GitConflict(BaseModel):
    """Available base, ours, and theirs stages, including add/delete conflicts."""

    path: WorkspacePath
    base: GitBlob | None = None
    ours: GitBlob | None = None
    theirs: GitBlob | None = None


class GitConflicts(BaseModel):
    """Conflict stages captured from the current index without resolving them."""

    operation: GitOperation | None
    files: list[GitConflict]
    process_ids: list[int] = Field(default_factory=list)


class GitMutation(BaseModel):
    """An execution outcome and actual resulting repository state, including conflicts."""

    head_before: str | None
    head_after: str | None
    operation: GitOperation | None
    conflicts: list[WorkspacePath]
    changes: list[GitEntry]
    process_ids: list[int]
    error: str | None = None
    synchronization_error: str | None = None
    updates: list[str] = Field(default_factory=list)


class GitStashResult(GitMutation):
    """A stash action outcome with optional retained stash records in the same schema."""

    stashes: list[GitStash] = Field(default_factory=list)
