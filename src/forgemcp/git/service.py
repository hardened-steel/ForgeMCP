"""Repository-scoped Git scenarios and SDK entrypoints shared by stdio and the admin panel."""

# Explicit local SDK handlers keep the public schemas inspectable without a tool factory.
# pylint: disable=too-many-lines

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, cast

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import (
    CallToolResult,
    Completion,
    CompletionArgument,
    CompletionContext,
    PromptReference,
    ResourceTemplateReference,
    ToolAnnotations,
)
from pydantic import Field

from forgemcp.assets import IconFile, Widget
from forgemcp.clangd.service import ClangdService, FileChange
from forgemcp.cmake.service import CMakeService
from forgemcp.completion import Complete
from forgemcp.process.models import ProcessTimeout
from forgemcp.progress import progress
from forgemcp.text import tool_result
from forgemcp.toolchain.errors import ToolchainError, ToolCommandError
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.tools import git
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import WorkspaceService

from . import parsers
from .errors import GitError
from .models import (
    GitBlame,
    GitBlob,
    GitCommitDetails,
    GitConflict,
    GitConflicts,
    GitDiff,
    GitHistory,
    GitMutation,
    GitOperation,
    GitRef,
    GitRefs,
    GitRemote,
    GitRemotes,
    GitStash,
    GitStashResult,
    GitStatus,
)
from .text import render


@dataclass
class Invocation:
    """Task-local command provenance and progress for one SDK request."""

    methods: git.Methods
    report: Callable[..., Awaitable[None]]
    process_ids: list[int] = field(default_factory=list)
    updates: int = 0


invocation: ContextVar[Invocation] = ContextVar("forgemcp_git_invocation")


def literal_identifier(value: str, label: str) -> str:
    """Keep literal identifiers from becoming command options or NUL-delimited records."""
    if not value or value.startswith("-") or "\0" in value or "\n" in value or "\r" in value:
        raise GitError(f"{label} must be a nonempty literal identifier without an option prefix.")
    return value


def redact(value: str) -> str:
    """Remove URL user-info before exposing configured addresses or command errors."""
    return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@", r"\1[redacted]@", value)


class GitService:
    """Inspect and modify the Git repository rooted at this workspace.

    Inspect status/diff before staging and committing. Pull defaults to fast-forward
    only; select merge/rebase explicitly. Conflicts remain resumable Git operations.
    Hooks run normally. Tools never create or manage worktrees or execute arbitrary commands.
    """

    ICON = IconFile("icons/git.svg")
    WIDGET = Widget("assets/git-result.html")
    STATUS_URI = "forgemcp://git/status"
    COMMIT_URI = "forgemcp://git/commits/{revision}"
    COMMIT_FORMAT = "%H%x00%P%x00%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI%x00%B"
    MAX_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        workspace: WorkspaceService,
        toolchains: ToolchainService,
        cmake: CMakeService,
        analysis: ClangdService,
        *,
        toolset: str = "system",
    ) -> None:
        """Bind existing services and protect Git's own project metadata from file tools."""
        self.workspace = workspace
        self.toolchains = toolchains
        self.cmake = cmake
        self.analysis = analysis
        self.toolset = toolset
        self.last_status: GitStatus | None = None
        self.commit_snapshots: dict[str, GitHistory] = {}
        workspace.protect_path(WorkspacePath("project/.git"))

    @asynccontextmanager
    async def request(self, ctx: Context) -> AsyncGenerator[None]:
        """Serialize repository requests with Workspace and retain per-call process provenance."""
        report = progress(ctx, interval=self.workspace.progress_interval)
        await report(0, message="Opening Git repository")
        try:
            async with self.workspace.serialized_operation():
                selected = [
                    item
                    for item in self.toolchains.list_toolsets()
                    if self.toolset in (item.id, item.name)
                ]
                if len(selected) != 1:
                    raise GitError(f"Expected one Git toolset matching {self.toolset!r}.")
                tool = self.toolchains.get_tool(selected[0].id, "git")
                if tool is None:
                    raise GitError("The selected toolset has no Git executable.")
                token = invocation.set(Invocation(cast(git.Methods, tool.methods), report))
                try:
                    async with asyncio.timeout(620):
                        await self.repository()
                        yield
                    call = invocation.get()
                    call.updates += 1
                    await report(call.updates, message="Git request completed")
                finally:
                    invocation.reset(token)
        except (GitError, ToolchainError) as error:
            raise ToolError(str(error)) from error
        except TimeoutError as error:
            raise ToolError(
                "Git request exceeded its total deadline; inspect status before retrying.",
            ) from error

    async def command(
        self,
        arguments: Sequence[str],
        *,
        check: bool = True,
        read_only: bool = True,
        stdin: str | None = None,
    ) -> git.CommandResult:
        """Execute internally constructed argv with bounded output and tracked progress."""
        call = invocation.get()
        call.updates += 1
        await call.report(call.updates, message=f"Git {arguments[0]}")
        try:
            result = await call.methods["run"](
                self.workspace.root,
                arguments,
                timeout=ProcessTimeout(total=300),
                stdin=stdin,
                max_bytes=self.MAX_BYTES,
                read_only=read_only,
            )
        except ToolCommandError as error:
            if error.process_id is not None:
                call.process_ids.append(error.process_id)
            raise GitError(
                f"{redact(str(error))} Process: {error.process_id}. "
                "Inspect repository status before retrying a mutation.",
            ) from error
        call.process_ids.append(result.process_id)
        if check and result.return_code:
            reason = redact(result.stderr.strip()) or f"Git exited with {result.return_code}."
            raise GitError(f"{reason} Process: {result.process_id}.")
        return result

    async def repository(self) -> None:
        """Reject bare or ancestor repositories so commands cannot mutate outside this worktree."""
        result = await self.command(["rev-parse", "--show-toplevel"])
        root = Path(parsers.utf8(result.stdout).rstrip("\r\n")).resolve()
        if root != self.workspace.root:
            raise GitError("The configured workspace must be the Git worktree root.")

    async def revision(self, value: str) -> str:
        """Resolve a single commit to its immutable OID before using it in another command."""
        literal_identifier(value, "Revision")
        result = await self.command(
            ["rev-parse", "--verify", "--end-of-options", value + "^{commit}"],
        )
        return parsers.utf8(result.stdout).strip()

    def paths(self, paths: Sequence[WorkspacePath]) -> list[str]:
        """Accept only explicit project paths and reject links or Git administrative paths."""
        if not paths or len(paths) > 1000:
            raise GitError("Select between 1 and 1000 explicit project paths.")
        result = []
        for path in paths:
            if path.area != "project" or path.relative == ".":
                raise GitError("Select explicit paths inside project/, not a root or storage path.")
            if any(part.casefold() == ".git" for part in Path(path.relative).parts):
                raise GitError("Git administrative paths cannot be selected as source files.")
            native = self.workspace.root / path.relative
            if not native.parent.resolve().is_relative_to(self.workspace.root):
                raise GitError("Git paths cannot traverse links outside the project.")
            result.append(path.relative)
        return result

    async def operation(self) -> GitOperation | None:
        """Recover stopped noninteractive operations from Git-owned pseudorefs."""
        for reference, name in (
            ("rebase-merge/orig-head", "rebase"),
            ("rebase-apply/orig-head", "rebase"),
            ("REBASE_HEAD", "rebase"),
            ("MERGE_HEAD", "merge"),
            ("CHERRY_PICK_HEAD", "cherry_pick"),
            ("REVERT_HEAD", "revert"),
        ):
            result = await self.command(
                ["rev-parse", "-q", "--symbolic-full-name", "--verify", reference],
                check=False,
            )
            resolved = parsers.utf8(result.stdout).strip()
            if result.return_code == 0 and resolved == reference:
                return cast(GitOperation, name)
            if "ambiguous" in result.stderr:
                raise GitError(
                    "An operation pseudoref is ambiguous with a branch; inspect refs first."
                )
        return None

    async def status(self) -> GitStatus:
        """Capture actual index/worktree states and recover unfinished Git operations."""
        result = await self.command(
            ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"],
        )
        snapshot = parsers.status(result.stdout)
        snapshot.operation = await self.operation()
        snapshot.process_ids = invocation.get().process_ids.copy()
        self.last_status = snapshot.model_copy(deep=True)
        return snapshot

    async def mutate(
        self,
        arguments: Sequence[str],
        *,
        source_changes: bool = False,
        stdin: str | None = None,
    ) -> GitMutation:
        """Run a mutation once and reconcile source consumers even after partial failure."""
        before = await self.status()
        completed = False
        sync_error = None
        result = None
        after = GitStatus()
        changes: list[FileChange] | None = None
        async with self.analysis.source_change():
            context = await self.cmake.begin_source_change()
            try:
                result = await self.command(arguments, check=False, read_only=False, stdin=stdin)
                completed = True
                try:
                    after = await self.status()
                    changes = await self.source_files(before, after) if source_changes else []
                except (GitError, ToolchainError):
                    sync_error = (
                        "Git completed, but its resulting source state could not be inspected."
                    )
            finally:
                try:
                    await self.cmake.finish_source_change(
                        context,
                        structural=source_changes,
                        uncertain=not completed or changes is None,
                    )
                    await self.analysis.refresh_source_changes(changes)
                # A secondary synchronization error must not conceal a completed commit/push.
                except Exception as error:  # pylint: disable=broad-exception-caught
                    await self.analysis.close_sessions()
                    sync_error = f"Source synchronization failed: {type(error).__name__}."
        return GitMutation(
            head_before=before.head,
            head_after=after.head,
            operation=after.operation,
            conflicts=[
                entry.path
                for entry in after.entries
                if "U" in (entry.index_status, entry.worktree_status)
                or entry.index_status + entry.worktree_status in ("AA", "DD")
            ],
            changes=after.entries,
            process_ids=invocation.get().process_ids.copy(),
            error=(
                (redact(result.stderr.strip()) or f"Git exited with {result.return_code}.")
                if result.return_code
                else None
            ),
            synchronization_error=sync_error,
            updates=(
                [redact(line) for line in parsers.utf8(result.stdout).splitlines() if "\t" in line]
                if arguments[0] == "push"
                else []
            ),
        )

    async def source_files(self, before: GitStatus, after: GitStatus) -> list[FileChange]:
        """Combine dirty paths and committed tree changes without scanning the project."""
        affected = {entry.path for entry in [*before.entries, *after.entries]}
        if before.head != after.head and after.head is not None:
            if before.head is not None:
                command = ["diff", "--raw", "--no-abbrev", "-z", before.head, after.head, "--"]
            else:
                command = [
                    "diff-tree", "--root", "--no-commit-id", "-r",
                    "--raw", "--no-abbrev", "-z", after.head,
                ]
            result = await self.command(command)
            for change in parsers.changes(result.stdout):
                affected.add(change.path)
                if change.original_path is not None:
                    affected.add(change.original_path)
        changes = []
        for path in sorted(affected):
            try:
                (self.workspace.root / path.relative).lstat()
            except FileNotFoundError:
                kind = 3
            except OSError as error:
                raise GitError("Cannot inspect a changed source path.") from error
            else:
                kind = 2
            changes.append(FileChange(path, kind))
        return changes

    async def diff(
        self,
        mode: Literal["unstaged", "staged", "revision"],
        base: str | None,
        target: str | None,
        paths: Sequence[WorkspacePath] | None,
        context: int,
    ) -> GitDiff:
        """Read native raw metadata and patch output for the same resolved comparison."""
        arguments = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--find-renames"]
        if mode != "revision" and (base is not None or target is not None):
            raise GitError("base and target apply only to mode=revision.")
        if mode == "staged":
            arguments.append("--cached")
        elif mode == "revision":
            if base is None:
                raise GitError("Revision comparisons require base.")
            base = await self.revision(base)
            arguments.append(base)
            if target is not None:
                target = await self.revision(target)
                arguments.append(target)
        suffix = ["--", *(self.paths(paths) if paths is not None else [])]
        raw = await self.command([*arguments, "--raw", "--no-abbrev", "-z", *suffix])
        patch = await self.command([*arguments, f"--unified={context}", *suffix])
        text = patch.stdout.decode("utf-8", errors="replace")
        return GitDiff(
            mode=mode,
            base=base,
            target=target,
            files=parsers.changes(raw.stdout),
            patch=text,
            had_decoding_errors=text.encode("utf-8") != patch.stdout,
            process_ids=invocation.get().process_ids.copy(),
        )

    async def history(
        self,
        revision: str,
        limit: int,
        skip: int,
        path: WorkspacePath | None,
    ) -> GitHistory:
        """Return stable commit pages, treating an unborn default HEAD as empty history."""
        if revision == "HEAD" and (await self.status()).head is None:
            return GitHistory(
                revision=None,
                commits=[],
                process_ids=invocation.get().process_ids.copy(),
            )
        oid = await self.revision(revision)
        result = await self.command(
            [
                "-c",
                "i18n.logOutputEncoding=UTF-8",
                "log",
                "-z",
                f"--format={self.COMMIT_FORMAT}",
                f"--max-count={limit + 1}",
                f"--skip={skip}",
                oid,
                "--",
                *(self.paths([path]) if path else []),
            ],
        )
        commits = parsers.commits(result.stdout)
        for commit in commits:
            self.commit_snapshots[commit.oid] = GitHistory(
                revision=commit.oid,
                commits=[commit],
            )
        if commits:
            self.commit_snapshots[revision] = self.commit_snapshots[commits[0].oid]
        return GitHistory(
            revision=oid,
            commits=commits[:limit],
            next_skip=skip + limit if len(commits) > limit else None,
            process_ids=invocation.get().process_ids.copy(),
        )

    async def refs(self, tags: bool = False) -> GitRefs:
        """Capture branch/tag refs without consulting a remote server."""
        result = await self.command(
            [
                "for-each-ref",
                "--sort=refname",
                "--format=%(refname)%00%(objectname)%00%(upstream)%00%(HEAD)",
                *(["refs/tags/"] if tags else ["refs/heads/", "refs/remotes/"]),
            ],
        )
        refs = []
        for line in result.stdout.splitlines():
            fields = line.split(b"\0")
            if len(fields) != 4:
                raise GitError("Cannot parse Git reference records.")
            name, oid, upstream, current = map(parsers.utf8, fields)
            refs.append(
                GitRef(name=name, oid=oid, upstream=upstream or None, current=current == "*"),
            )
        return GitRefs(refs=refs, process_ids=invocation.get().process_ids.copy())

    async def remote(self, name: str) -> str:
        """Require a configured remote name rather than accepting a URL or helper command."""
        literal_identifier(name, "Remote")
        result = await self.command(["remote"])
        if name not in parsers.utf8(result.stdout).splitlines():
            raise GitError(f"Unknown configured remote {name!r}.")
        return name

    # The SDK requires all explicitly typed decorated entrypoints to remain local.
    # pylint: disable=too-many-locals,too-many-statements
    def register(
        self,
        mcp: MCPServer,
        apps: Apps,
        complete: Complete,
    ) -> None:
        """Expose typed Git tools through one packaged App and retained text resources."""
        icon = self.ICON.icon
        read_only = ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            open_world_hint=False,
        )
        writes = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
        network = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
        apps.add_html_resource(self.WIDGET.uri, self.WIDGET.content)

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=read_only)
        async def git_status(ctx: Context) -> Annotated[CallToolResult, GitStatus]:
            """Inspect branch, index/worktree changes, and unfinished Git operations."""
            async with self.request(ctx):
                result = await self.status()
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=read_only)
        async def git_diff(  # pylint: disable=too-many-arguments
            ctx: Context,
            *,
            mode: Literal["unstaged", "staged", "revision"] = "unstaged",
            base: str | None = None,
            target: str | None = None,
            paths: list[WorkspacePath] | None = None,
            context: Annotated[int, Field(ge=0, le=100)] = 3,
        ) -> Annotated[CallToolResult, GitDiff]:
            """Compare index/worktree or revisions with native patches and typed file metadata."""
            async with self.request(ctx):
                result = await self.diff(mode, base, target, paths, context)
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=read_only)
        async def git_log(
            ctx: Context,
            revision: str = "HEAD",
            limit: Annotated[int, Field(ge=1, le=200)] = 30,
            skip: Annotated[int, Field(ge=0, le=100000)] = 0,
            path: WorkspacePath | None = None,
        ) -> Annotated[CallToolResult, GitHistory]:
            """Read a bounded commit page; reuse its resolved revision for later pages."""
            async with self.request(ctx):
                result = await self.history(revision, limit, skip, path)
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=read_only)
        async def git_show(
            ctx: Context,
            revision: str = "HEAD",
        ) -> Annotated[CallToolResult, GitCommitDetails]:
            """Read a commit and its first-parent patch, including root commits."""
            async with self.request(ctx):
                history = await self.history(revision, 1, 0, None)
                if not history.commits:
                    raise GitError("An unborn HEAD has no commit to show.")
                commit = history.commits[0]
                if commit.parents:
                    diff = await self.diff("revision", commit.parents[0], commit.oid, None, 3)
                else:
                    raw = await self.command(
                        [
                            "diff-tree",
                            "--root",
                            "--no-commit-id",
                            "-r",
                            "--raw",
                            "--no-abbrev",
                            "-z",
                            commit.oid,
                        ],
                    )
                    patch = await self.command(
                        [
                            "show",
                            "--format=",
                            "--no-ext-diff",
                            "--no-textconv",
                            "--no-color",
                            commit.oid,
                            "--",
                        ],
                    )
                    diff = GitDiff(
                        mode="revision",
                        target=commit.oid,
                        files=parsers.changes(raw.stdout),
                        patch=patch.stdout.decode("utf-8", errors="replace"),
                        had_decoding_errors=patch.stdout.decode(
                            "utf-8", errors="replace"
                        ).encode("utf-8") != patch.stdout,
                        process_ids=invocation.get().process_ids.copy(),
                    )
                result = GitCommitDetails(commit=commit, diff=diff)
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=read_only)
        async def git_blame(
            path: WorkspacePath,
            ctx: Context,
            revision: str = "HEAD",
            start_line: Annotated[int, Field(ge=1)] = 1,
            end_line: Annotated[int | None, Field(ge=1)] = None,
        ) -> Annotated[CallToolResult, GitBlame]:
            """Attribute a committed UTF-8 file or inclusive line range to its commits."""
            async with self.request(ctx):
                selected = self.paths([path])[0]
                if end_line is not None and end_line < start_line:
                    raise GitError("end_line must not precede start_line.")
                oid = await self.revision(revision)
                command = await self.command(
                    [
                        "blame",
                        "--line-porcelain",
                        "-L",
                        f"{start_line},{end_line or ''}",
                        oid,
                        "--",
                        selected,
                    ],
                )
                result = GitBlame(
                    path=path,
                    revision=oid,
                    lines=parsers.blame(command.stdout),
                    process_ids=invocation.get().process_ids.copy(),
                )
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=read_only)
        async def git_branches(ctx: Context) -> Annotated[CallToolResult, GitRefs]:
            """List local and remote-tracking branches with upstream and current-branch metadata."""
            async with self.request(ctx):
                result = await self.refs()
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=read_only)
        async def git_tags(ctx: Context) -> Annotated[CallToolResult, GitRefs]:
            """List lightweight and annotated tag references without changing them."""
            async with self.request(ctx):
                result = await self.refs(tags=True)
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=read_only)
        async def git_remotes(ctx: Context) -> Annotated[CallToolResult, GitRemotes]:
            """List configured remotes with credential-redacted fetch/push URLs."""
            async with self.request(ctx):
                names = await self.command(["remote"])
                remotes = []
                for name in parsers.utf8(names.stdout).splitlines():
                    fetch = await self.command(["remote", "get-url", "--all", name])
                    push = await self.command(["remote", "get-url", "--push", "--all", name])
                    remotes.append(
                        GitRemote(
                            name=name,
                            fetch_urls=[
                                redact(url) for url in parsers.utf8(fetch.stdout).splitlines()
                            ],
                            push_urls=[
                                redact(url) for url in parsers.utf8(push.stdout).splitlines()
                            ],
                        ),
                    )
                result = GitRemotes(
                    remotes=remotes,
                    process_ids=invocation.get().process_ids.copy(),
                )
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_stage(
            paths: list[WorkspacePath],
            ctx: Context,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Stage explicit literal paths, including additions, modifications, and deletions."""
            async with self.request(ctx):
                result = await self.mutate(
                    ["add", "--all", "--", *self.paths(paths)],
                    source_changes=True,
                )
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_unstage(
            paths: list[WorkspacePath],
            ctx: Context,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Unstage explicit paths while preserving working files, including unborn HEAD."""
            async with self.request(ctx):
                selected = self.paths(paths)
                state = await self.status()
                command = (
                    ["restore", "--staged", "--", *selected]
                    if state.head
                    else [
                        "rm",
                        "--cached",
                        "--ignore-unmatch",
                        "--",
                        *selected,
                    ]
                )
                result = await self.mutate(command, source_changes=True)
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_commit(
            message: str,
            ctx: Context,
            amend: bool = False,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Commit the current index with a supplied message and normal repository hooks."""
            async with self.request(ctx):
                if not message.strip() or "\0" in message:
                    raise GitError("A nonempty commit message without NUL is required.")
                result = await self.mutate(
                    ["commit", *(["--amend"] if amend else []), "--file=-"],
                    source_changes=True,
                    stdin=message,
                )
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_restore(
            paths: list[WorkspacePath],
            ctx: Context,
            source: str | None = None,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Restore selected working files from the index or an explicit commit."""
            async with self.request(ctx):
                options = [f"--source={await self.revision(source)}"] if source is not None else []
                result = await self.mutate(
                    ["restore", "--worktree", *options, "--", *self.paths(paths)],
                    source_changes=True,
                )
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_branch(
            action: Literal["create", "delete"],
            name: str,
            ctx: Context,
            start_point: str = "HEAD",
            force: bool = False,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Create a branch or delete one, requiring explicit force for unmerged deletion."""
            async with self.request(ctx):
                literal_identifier(name, "Branch")
                await self.command(["check-ref-format", "--branch", name])
                if action == "create":
                    if force:
                        raise GitError("force applies only to explicit branch deletion.")
                    command = ["branch", name, await self.revision(start_point)]
                else:
                    command = ["branch", "-D" if force else "-d", name]
                result = await self.mutate(command, source_changes=True)
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_switch(
            branch: str,
            ctx: Context,
            create: bool = False,
            start_point: str | None = None,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Switch or create a branch without force or automatic stash."""
            async with self.request(ctx):
                literal_identifier(branch, "Branch")
                await self.command(["check-ref-format", "--branch", branch])
                if start_point is not None and not create:
                    raise GitError("start_point requires create=true.")
                command = ["switch", "--no-guess", *(["-c"] if create else []), branch]
                if start_point is not None:
                    command.append(await self.revision(start_point))
                result = await self.mutate(command, source_changes=True)
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_stash(
            action: Literal["list", "save", "apply", "pop", "drop"],
            ctx: Context,
            reference: str = "stash@{0}",
            message: str | None = None,
            include_untracked: bool = False,
        ) -> Annotated[CallToolResult, GitStashResult]:
            """List, save, apply, pop, or drop stashes without automatically hiding other edits."""
            async with self.request(ctx):
                if action == "list":
                    output = await self.command(
                        ["stash", "list", "-z", "--format=%gd%x00%H%x00%gs"],
                    )
                    fields = output.stdout.split(b"\0")
                    if fields[-1] == b"":
                        fields.pop()
                    if len(fields) % 3:
                        raise GitError("Cannot parse Git stash records.")
                    state = await self.status()
                    result = GitStashResult(
                        head_before=state.head,
                        head_after=state.head,
                        operation=state.operation,
                        conflicts=[],
                        changes=state.entries,
                        stashes=[
                            GitStash(
                                reference=parsers.utf8(fields[index]),
                                oid=parsers.utf8(fields[index + 1]),
                                message=parsers.utf8(fields[index + 2]),
                            )
                            for index in range(0, len(fields), 3)
                        ],
                        process_ids=invocation.get().process_ids.copy(),
                    )
                    return tool_result(result, render(result))
                if action == "save":
                    if message is not None and "\0" in message:
                        raise GitError("Stash messages cannot contain NUL.")
                    command = [
                        "stash",
                        "push",
                        *(["--include-untracked"] if include_untracked else []),
                        *(["--message", message] if message is not None else []),
                    ]
                else:
                    if not re.fullmatch(r"stash@\{[0-9]+\}", reference):
                        raise GitError("Select a stash reflog reference such as stash@{0}.")
                    command = ["stash", action, reference]
                changed = await self.mutate(command, source_changes=True)
                result = GitStashResult(**changed.model_dump())
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=network)
        async def git_fetch(
            ctx: Context,
            remote: str = "origin",
            prune: bool = False,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Fetch one configured remote; remote-tracking pruning is explicit."""
            async with self.request(ctx):
                name = await self.remote(remote)
                result = await self.mutate(
                    [
                        "fetch",
                        "--no-recurse-submodules",
                        "--prune" if prune else "--no-prune",
                        name,
                    ],
                    source_changes=True,
                )
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=network)
        async def git_pull(
            ctx: Context,
            remote: str | None = None,
            branch: str | None = None,
            strategy: Literal["ff-only", "merge", "rebase"] = "ff-only",
        ) -> Annotated[CallToolResult, GitMutation]:
            """Pull without auto-stash; fast-forward only is the default integration strategy."""
            async with self.request(ctx):
                if branch is not None and remote is None:
                    raise GitError("An explicit branch requires an explicit configured remote.")
                command = ["pull", "--no-recurse-submodules", "--no-autostash"]
                if strategy == "rebase":
                    command.extend(["--rebase", "--ff"])
                else:
                    command.extend(
                        [
                            "--no-rebase",
                            "--ff-only" if strategy == "ff-only" else "--ff",
                            "--no-edit",
                        ],
                    )
                if remote is not None:
                    command.append(await self.remote(remote))
                if branch is not None:
                    literal_identifier(branch, "Remote branch")
                    await self.command(["check-ref-format", "refs/heads/" + branch])
                    command.append("refs/heads/" + branch)
                result = await self.mutate(command, source_changes=True)
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=network)
        async def git_push(
            ctx: Context,
            remote: str = "origin",
            branch: str | None = None,
            expected_remote_oid: str | None = None,
            set_upstream: bool = False,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Push HEAD to one branch; replacing history requires the expected remote OID."""
            async with self.request(ctx):
                name = await self.remote(remote)
                state = await self.status()
                target = branch or state.branch
                if target is None:
                    raise GitError("Detached HEAD requires an explicit destination branch.")
                literal_identifier(target, "Destination branch")
                await self.command(["check-ref-format", "refs/heads/" + target])
                command = ["push", "--porcelain", "--no-follow-tags", "--recurse-submodules=no"]
                if expected_remote_oid is not None:
                    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", expected_remote_oid):
                        raise GitError("expected_remote_oid must be a full remote object ID.")
                    command.append(f"--force-with-lease=refs/heads/{target}:{expected_remote_oid}")
                if set_upstream:
                    command.append("--set-upstream")
                command.extend([name, f"HEAD:refs/heads/{target}"])
                result = await self.mutate(command, source_changes=True)
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_merge(
            revision: str,
            ctx: Context,
            message: str | None = None,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Merge one commit, preserving conflicts for explicit resolution and continuation."""
            async with self.request(ctx):
                if message is not None and "\0" in message:
                    raise GitError("Merge message cannot contain NUL.")
                command = ["merge", "--no-autostash", "--no-edit"]
                if message is not None:
                    command.extend(["--message", message])
                command.append(await self.revision(revision))
                result = await self.mutate(command, source_changes=True)
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_rebase(revision: str, ctx: Context) -> Annotated[CallToolResult, GitMutation]:
            """Rebase onto one resolved commit without interactive editing or automatic stash."""
            async with self.request(ctx):
                result = await self.mutate(
                    ["rebase", "--no-autostash", await self.revision(revision)],
                    source_changes=True,
                )
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_cherry_pick(
            revisions: list[str],
            ctx: Context,
            mainline: Annotated[int | None, Field(ge=1)] = None,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Cherry-pick an explicit ordered commit list with resumable conflict state."""
            async with self.request(ctx):
                if not revisions or len(revisions) > 100:
                    raise GitError("Select between 1 and 100 commits.")
                resolved = [await self.revision(value) for value in revisions]
                result = await self.mutate(
                    [
                        "cherry-pick",
                        *(["--mainline", str(mainline)] if mainline else []),
                        *resolved,
                    ],
                    source_changes=True,
                )
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_revert(
            revisions: list[str],
            ctx: Context,
            mainline: Annotated[int | None, Field(ge=1)] = None,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Revert an ordered commit list without opening an editor."""
            async with self.request(ctx):
                if not revisions or len(revisions) > 100:
                    raise GitError("Select between 1 and 100 commits.")
                resolved = [await self.revision(value) for value in revisions]
                result = await self.mutate(
                    [
                        "revert",
                        "--no-edit",
                        *(["--mainline", str(mainline)] if mainline else []),
                        *resolved,
                    ],
                    source_changes=True,
                )
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=read_only)
        async def git_conflicts(
            ctx: Context,
            paths: list[WorkspacePath] | None = None,
        ) -> Annotated[CallToolResult, GitConflicts]:
            """Read base/ours/theirs index stages without resolving conflicts."""
            async with self.request(ctx):
                command = await self.command(
                    [
                        "ls-files",
                        "--unmerged",
                        "-z",
                        "--",
                        *(self.paths(paths) if paths is not None else []),
                    ],
                )
                files: dict[WorkspacePath, GitConflict] = {}
                for record in command.stdout.split(b"\0"):
                    if not record:
                        continue
                    header, _, raw_path = record.partition(b"\t")
                    mode, oid, stage = parsers.utf8(header).split(" ")
                    path = parsers.qualified(raw_path)
                    file = files.setdefault(path, GitConflict(path=path))
                    if mode == "160000":
                        setattr(
                            file,
                            {"1": "base", "2": "ours", "3": "theirs"}[stage],
                            GitBlob(oid=oid, mode=mode, binary=True, text=None),
                        )
                        continue
                    blob = await self.command(["cat-file", "blob", oid])
                    try:
                        text = blob.stdout.decode("utf-8")
                    except UnicodeDecodeError:
                        text = None
                    binary = b"\0" in blob.stdout or text is None
                    setattr(
                        file,
                        {"1": "base", "2": "ours", "3": "theirs"}[stage],
                        GitBlob(oid=oid, mode=mode, binary=binary, text=None if binary else text),
                    )
                result = GitConflicts(
                    operation=await self.operation(),
                    files=list(files.values()),
                    process_ids=invocation.get().process_ids.copy(),
                )
                return tool_result(result, render(result))

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=writes)
        async def git_continue(
            action: Literal["continue", "abort", "skip"],
            ctx: Context,
            operation: GitOperation | None = None,
        ) -> Annotated[CallToolResult, GitMutation]:
            """Continue, abort, or skip a stopped operation after explicitly staging resolutions."""
            async with self.request(ctx):
                current = await self.operation()
                selected = operation or current
                if selected is None or (current is not None and selected != current):
                    raise GitError(
                        "Select the current unfinished merge/rebase/cherry-pick/revert operation.",
                    )
                if selected == "merge" and action == "skip":
                    raise GitError("Merge supports continue or abort, not skip.")
                command = [
                    "-c",
                    "core.editor=true",
                    "-c",
                    "sequence.editor=true",
                    selected.replace("_", "-"),
                    f"--{action}",
                ]
                result = await self.mutate(command, source_changes=True)
                return tool_result(result, render(result))

        @mcp.resource(self.STATUS_URI, mime_type="text/plain", icons=[icon])
        def git_status_resource() -> str:
            """Read captured status without executing Git in a resource handler."""
            if self.last_status is None:
                raise ResourceNotFoundError("Run git_status to capture repository state first.")
            return "Last captured Git status:\n" + render(self.last_status)

        @mcp.resource(self.COMMIT_URI, mime_type="text/plain", icons=[icon])
        def git_commit_resource(revision: str) -> str:
            """Read commit metadata previously captured by git_log or git_show."""
            try:
                return render(self.commit_snapshots[revision])
            except KeyError as error:
                raise ResourceNotFoundError(
                    "Run git_log or git_show for this revision first.",
                ) from error

        async def git_completion(
            ref: PromptReference | ResourceTemplateReference,
            argument: CompletionArgument,
            context: CompletionContext | None,
        ) -> Completion | None:
            """Complete retained branch/tag names without launching Git during completion."""
            del context
            if not isinstance(ref, ResourceTemplateReference) or ref.uri != self.COMMIT_URI:
                return None
            values = [name for name in self.commit_snapshots if name.startswith(argument.value)]
            return Completion(values=values[:100], total=len(values), has_more=len(values) > 100)

        complete.add_completion(git_completion)
