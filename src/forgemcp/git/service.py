"""Fixed-grammar, read-only Git service with no public host-path boundary."""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from forgemcp.core.config import ForgeConfig
from forgemcp.git.errors import GitOutputError, GitRequestError, GitUnavailableError
from forgemcp.git.models import (
    MAX_GIT_BLAME_RANGES,
    MAX_GIT_BRANCHES,
    MAX_GIT_COMMITS,
    MAX_GIT_PARENTS,
    MAX_GIT_PATHS,
    MAX_GIT_STATUS_RECORDS,
    GitBlameRange,
    GitBlameResult,
    GitBranch,
    GitBranchList,
    GitCommit,
    GitDiffResult,
    GitFileStatus,
    GitLogResult,
    GitPatchSummary,
    GitRepositoryAvailability,
    GitShowCommitResult,
    GitStatus,
)
from forgemcp.models import ProcessResult
from forgemcp.processes import ProcessError
from forgemcp.toolchain import ToolchainDiscoveryService
from forgemcp.workspace import WorkspaceError, WorkspaceService


_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_VERSION = re.compile(r"^git version ([0-9]+(?:\.[0-9]+){1,3}(?:[A-Za-z0-9._+-]*)?)\s*$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MAX_CURSOR_CACHE = 32
_MAX_CACHED_BRANCHES = 128
_MAX_CACHED_PATHS = 128


@dataclass(frozen=True, slots=True)
class _Qualification:
    executable: Path
    version: str


@dataclass(frozen=True, slots=True)
class _Cursor:
    skip: int


class GitService:
    """Application-scoped Git Intelligence Phase 1 service.

    All command construction is authored here.  The only dynamic values are
    strictly validated OIDs, bounded numerics, and revalidated workspace paths;
    no caller receives a generic revision, config, argv, shell, or environment
    capability.
    """

    __slots__ = (
        "_config", "_workspace", "_runtime", "_toolchain", "_qualification",
        "_qualification_error", "_cached_status", "_cached_at", "_cursors",
        "_cached_branches", "_cached_paths", "_invalidated",
    )

    def __init__(
        self,
        config: ForgeConfig,
        workspace: WorkspaceService,
        process_runtime: object,
        toolchain: ToolchainDiscoveryService,
    ) -> None:
        self._config = config
        self._workspace = workspace
        self._runtime = process_runtime
        self._toolchain = toolchain
        self._qualification: _Qualification | None = None
        self._qualification_error: str | None = None
        self._cached_status: GitStatus | None = None
        self._cached_at: datetime | None = None
        self._cursors: dict[str, _Cursor] = {}
        self._cached_branches: tuple[str, ...] = ()
        self._cached_paths: tuple[str, ...] = ()
        self._invalidated = False

    @property
    def cached_status(self) -> GitStatus | None:
        """Return cached Git status only; this never invokes Git."""
        return self._cached_status

    @property
    def configured(self) -> bool:
        return self._config.git_path is not None

    @property
    def cached_at(self) -> datetime | None:
        return self._cached_at

    @property
    def qualified_version(self) -> str | None:
        return None if self._qualification is None else self._qualification.version

    def invalidate_after_workspace_mutation(self) -> None:
        """Drop the cache only after a committed Workspace mutation batch."""
        self._cached_status = None
        self._cached_at = None
        self._cursors.clear()
        self._cached_branches = ()
        self._cached_paths = ()
        self._invalidated = True

    def cached_completion_values(self, kind: str) -> tuple[str, ...]:
        """Return bounded cached values for legacy prompt/template completion only."""
        if kind == "branch":
            return self._cached_branches
        if kind == "cursor":
            return tuple(self._cursors)
        if kind == "path":
            return self._cached_paths
        return ()

    async def status(self) -> GitStatus:
        """Refresh porcelain-v2 status explicitly and replace the bounded cache."""
        repository = await self._repository_available()
        if repository is not None:
            self._cache_status(repository)
            return repository
        result = await self._run((
            "status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all", "--ignore-submodules=all",
        ), timeout_seconds=10.0)
        if result.timed_out or result.exit_code != 0:
            status = self._unavailable("Git repository status is unavailable.", error=True)
            self._cache_status(status)
            return status
        if result.stdout.truncated or result.stderr.truncated:
            # A missing final NUL makes any remaining porcelain record
            # ambiguous.  Do not return a trusted prefix as complete status.
            status = GitStatus(
                repository=GitRepositoryAvailability.AVAILABLE,
                git_available=True,
                git_configured=self.configured,
                staged_count=0, unstaged_count=0, untracked_count=0, conflicted_count=0,
                incomplete=True, truncated=True,
            )
            self._cache_status(status)
            return status
        try:
            status = self._parse_status(result.stdout.text)
        except GitOutputError:
            status = self._unavailable("Git returned malformed repository status.", error=True)
        self._cache_status(status)
        return status

    async def diff(
        self, *, scope: str, paths: Sequence[str] | None = None, context_lines: int | None = None,
    ) -> GitDiffResult:
        await self._require_repository()
        if scope not in {"unstaged", "staged"}:
            raise GitRequestError("scope must be unstaged or staged.")
        context = 3 if context_lines is None else context_lines
        if isinstance(context, bool) or not isinstance(context, int) or not 0 <= context <= 20:
            raise GitRequestError("context_lines must be an integer from zero through 20.")
        safe_paths = self._validate_paths(paths)
        command: list[str] = [
            "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
            "--ignore-submodules=all", f"--unified={context}",
        ]
        if scope == "staged":
            command.append("--cached")
        if safe_paths:
            command.append("--")
            command.extend(f":(literal){path}" for path in safe_paths)
        result = await self._run(tuple(command), timeout_seconds=20.0)
        if result.timed_out or result.exit_code != 0:
            raise GitUnavailableError("Git diff could not complete safely.")
        patch = result.stdout.text
        summary = GitPatchSummary(
            scope=scope,
            patch_truncated=result.stdout.truncated,
            binary_file_count=patch.count("Binary files ") + patch.count("GIT binary patch"),
            file_count=sum(1 for line in patch.splitlines() if line.startswith("diff --git ")),
            incomplete=result.stdout.truncated or result.stderr.truncated,
        )
        return GitDiffResult(patch=patch, summary=summary)

    async def log(self, *, limit: int = 20, cursor: str | None = None) -> GitLogResult:
        await self._require_repository()
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_GIT_COMMITS:
            raise GitRequestError("limit must be an integer from one through 100.")
        skip = 0
        if cursor is not None:
            token = self._validate_cursor(cursor)
            saved = self._cursors.get(token)
            if saved is None:
                raise GitRequestError("cursor is not available in this application session.")
            skip = saved.skip
        result = await self._run((
            "log", "--no-decorate", "--no-show-signature", "--no-notes",
            f"--max-count={limit}", f"--skip={skip}",
            "-z", "--format=%H%x00%P%x00%an%x00%aI%x00%s", "HEAD",
        ), timeout_seconds=15.0)
        if result.timed_out or result.exit_code != 0:
            raise GitUnavailableError("Git log is unavailable for this repository state.")
        if result.stdout.truncated or result.stderr.truncated:
            return GitLogResult(commits=(), truncated=True, incomplete=True)
        commits = self._parse_commit_stream(result.stdout.text)
        next_cursor = None
        if len(commits) == limit:
            next_cursor = self._new_cursor(skip + len(commits))
        return GitLogResult(commits=commits, next_cursor=next_cursor, truncated=False, incomplete=False)

    async def show_commit(self, oid: str) -> GitShowCommitResult:
        await self._require_repository()
        checked = self._validate_oid(oid)
        metadata_result = await self._run((
            "show", "--no-patch", "--no-decorate", "--no-show-signature", "--no-notes",
            "-z", "--format=%H%x00%P%x00%an%x00%aI%x00%s", checked,
        ), timeout_seconds=10.0)
        if (
            metadata_result.timed_out or metadata_result.exit_code != 0
            or metadata_result.stdout.truncated or metadata_result.stderr.truncated
        ):
            raise GitUnavailableError("The requested commit is unavailable.")
        commits = self._parse_commit_stream(metadata_result.stdout.text)
        if len(commits) != 1 or commits[0].oid != checked:
            raise GitOutputError("Git returned contradictory commit metadata.")
        patch_result = await self._run((
            "show", "--format=", "--no-decorate", "--no-show-signature", "--no-notes",
            "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames", "--ignore-submodules=all", checked,
        ), timeout_seconds=20.0)
        if patch_result.timed_out or patch_result.exit_code != 0:
            raise GitUnavailableError("The requested commit patch is unavailable.")
        patch = patch_result.stdout.text
        return GitShowCommitResult(
            commit=commits[0], patch=patch, patch_truncated=patch_result.stdout.truncated,
            binary_file_count=patch.count("Binary files ") + patch.count("GIT binary patch"),
            incomplete=patch_result.stdout.truncated or patch_result.stderr.truncated,
        )

    async def blame(
        self, *, path: str, start_line: int | None = None, end_line: int | None = None,
    ) -> GitBlameResult:
        await self._require_repository()
        safe_path = self._validate_paths((path,))[0]
        # Explicitly decode once through Workspace so the public contract is a
        # workspace text file; the source is intentionally never returned.
        try:
            self._workspace.read_text(safe_path)
        except WorkspaceError as error:
            raise GitRequestError("blame requires an existing UTF-8 workspace text file.") from error
        if (start_line is None) != (end_line is None):
            raise GitRequestError("start_line and end_line must be supplied together.")
        command: list[str] = ["blame", "--incremental", "--no-progress"]
        if start_line is not None and end_line is not None:
            if (
                isinstance(start_line, bool) or isinstance(end_line, bool)
                or not isinstance(start_line, int) or not isinstance(end_line, int)
                or not 1 <= start_line <= end_line <= 1_000_000
            ):
                raise GitRequestError("blame line ranges must be ordered positive values no greater than 1000000.")
            command.extend(("-L", f"{start_line},{end_line}"))
        # ``--`` makes even an option-looking validated filename data.  blame
        # does not consistently accept pathspec magic across supported Git
        # versions, so literal semantics are supplied by the delimiter.
        command.extend(("--", safe_path))
        result = await self._run(tuple(command), timeout_seconds=20.0)
        if result.timed_out or result.exit_code != 0:
            raise GitUnavailableError("Git blame is unavailable for this repository state.")
        if result.stdout.truncated or result.stderr.truncated:
            return GitBlameResult(path=safe_path, ranges=(), truncated=True, incomplete=True)
        ranges = self._parse_blame(result.stdout.text)
        return GitBlameResult(path=safe_path, ranges=ranges, truncated=False, incomplete=False)

    async def list_branches(self) -> GitBranchList:
        await self._require_repository()
        result = await self._run((
            "for-each-ref", "--sort=refname",
            "--format=%(HEAD)%00%(refname:short)%00%(objectname)%00%(upstream:short)%00",
            "refs/heads",
        ), timeout_seconds=10.0)
        if result.timed_out or result.exit_code != 0:
            raise GitUnavailableError("Local branch listing is unavailable.")
        if result.stdout.truncated or result.stderr.truncated:
            return GitBranchList(branches=(), truncated=True, incomplete=True)
        text = self._require_clean_protocol_text(result.stdout.text)
        branches: list[GitBranch] = []
        truncated = False
        lines = text.splitlines()
        if not lines:
            return GitBranchList(branches=(), truncated=False, incomplete=False)
        for line in lines:
            if not line.endswith("\0"):
                raise GitOutputError("Git returned malformed branch metadata.")
            fields = line[:-1].split("\0")
            if len(fields) != 4:
                raise GitOutputError("Git returned malformed branch metadata.")
            head, name, oid, upstream = fields
            if head not in {"*", " "} or not name or not _OID.fullmatch(oid):
                raise GitOutputError("Git returned malformed branch metadata.")
            if len(branches) >= MAX_GIT_BRANCHES:
                truncated = True
                continue
            branches.append(GitBranch(
                name=self._clean_untrusted(name, fallback="unnamed", maximum=1024), current=head == "*", oid=oid,
                upstream=None if not upstream else self._clean_untrusted(upstream, fallback="unknown", maximum=1024),
            ))
        self._cached_branches = tuple(branch.name for branch in branches[:_MAX_CACHED_BRANCHES])
        return GitBranchList(branches=tuple(branches), truncated=truncated, incomplete=truncated)

    async def _repository_available(self) -> GitStatus | None:
        qualification = await self._qualify()
        if qualification is None:
            return self._unavailable(self._qualification_error or "Git is not configured or available.")
        result = await self._run(("rev-parse", "--is-inside-work-tree", "--show-toplevel"), timeout_seconds=8.0)
        if result.timed_out or result.stdout.truncated or result.stderr.truncated:
            return self._unavailable("Git repository boundary could not be verified.", error=True)
        if result.exit_code != 0:
            return self._unavailable("The configured workspace is not a Git repository.")
        lines = result.stdout.text.splitlines()
        if len(lines) != 2 or lines[0] != "true":
            return self._unavailable("Git repository boundary could not be verified.", error=True)
        try:
            root = self._workspace.validate_reported_path(lines[1])
        except WorkspaceError:
            return self._unavailable("Git repository root is outside the configured workspace.")
        if root != ".":
            return self._unavailable("Git repository root does not match the configured workspace.")
        return None

    async def _require_repository(self) -> None:
        unavailable = await self._repository_available()
        if unavailable is not None:
            raise GitUnavailableError(unavailable.error or "Git repository is unavailable.")

    async def _qualify(self) -> _Qualification | None:
        if self._qualification is not None:
            return self._qualification
        if self._qualification_error is not None:
            return None
        candidate = self._toolchain.executable("git")
        if candidate is None:
            self._qualification_error = "Git is not configured or available."
            return None
        try:
            result = await self._run_raw(candidate, ("--version",), timeout_seconds=5.0)
        except (GitUnavailableError, ProcessError):
            self._qualification_error = "Git is not available through the configured Process Runtime."
            return None
        if result.timed_out or result.exit_code != 0 or result.stdout.truncated or result.stderr.truncated:
            self._qualification_error = "Git version qualification did not complete safely."
            return None
        match = _VERSION.fullmatch(result.stdout.text.strip())
        if match is None:
            self._qualification_error = "Git version qualification returned an unsupported response."
            return None
        self._qualification = _Qualification(candidate, match.group(1))
        return self._qualification

    async def _run(self, command: Sequence[str], *, timeout_seconds: float) -> ProcessResult:
        qualification = await self._qualify()
        if qualification is None:
            raise GitUnavailableError(self._qualification_error or "Git is unavailable.")
        return await self._run_raw(qualification.executable, command, timeout_seconds=timeout_seconds)

    async def _run_raw(self, executable: Path, command: Sequence[str], *, timeout_seconds: float) -> ProcessResult:
        argv = (
            str(executable), "--no-optional-locks",
            "-c", "core.fsmonitor=false",
            "-c", "credential.helper=",
            "-c", "diff.external=",
            "-c", "submodule.recurse=false",
            "--no-pager", *command,
        )
        runner = getattr(self._runtime, "run_git", None)
        try:
            if callable(runner):
                result = await runner(argv, cwd=".", timeout_seconds=timeout_seconds)
            else:  # Narrow fake-runtime compatibility; production always has run_git.
                result = await self._runtime.run(argv, cwd=".", timeout_seconds=timeout_seconds)
        except ProcessError as error:
            raise GitUnavailableError("Git is not available through the configured Process Runtime.") from error
        if not isinstance(result, ProcessResult):
            raise GitUnavailableError("Git Process Runtime returned an invalid result.")
        return result

    def _parse_status(self, text: str) -> GitStatus:
        text = self._require_clean_protocol_text(text)
        chunks = text.split("\0")
        if not chunks or chunks[-1] != "":
            raise GitOutputError("Git returned malformed porcelain status.")
        branch: str | None = None
        detached = False
        unborn = False
        head_oid: str | None = None
        ahead: int | None = None
        behind: int | None = None
        files: list[GitFileStatus] = []
        staged = unstaged = untracked = conflicted = 0
        truncated = False
        index = 0
        while index < len(chunks) - 1:
            entry = chunks[index]
            index += 1
            if entry.startswith("# "):
                key, separator, value = entry[2:].partition(" ")
                if not separator:
                    raise GitOutputError("Git returned malformed porcelain status.")
                if key == "branch.head":
                    detached = value == "(detached)"
                    branch = None if detached or value == "(unknown)" else self._clean_untrusted(
                        value, fallback="unknown", maximum=1024,
                    )
                elif key == "branch.oid":
                    unborn = value == "(initial)"
                    if not unborn:
                        if not _OID.fullmatch(value):
                            raise GitOutputError("Git returned malformed HEAD metadata.")
                        head_oid = value
                elif key == "branch.ab":
                    match = re.fullmatch(r"\+(\d+) -(\d+)", value)
                    if match is None:
                        raise GitOutputError("Git returned malformed ahead/behind metadata.")
                    ahead, behind = int(match.group(1)), int(match.group(2))
                continue
            record: GitFileStatus
            if entry.startswith("? "):
                path = self._safe_reported_path(entry[2:])
                record = GitFileStatus(path=path, staged_status="?", unstaged_status="?", untracked=True)
                untracked += 1
            elif entry.startswith(("1 ", "2 ", "u ")):
                if entry.startswith("1 "):
                    fields = entry.split(" ", 8)
                    if len(fields) != 9 or len(fields[1]) != 2:
                        raise GitOutputError("Git returned malformed porcelain status.")
                    code, path = fields[1], self._safe_reported_path(fields[8])
                    record = GitFileStatus(path=path, staged_status=code[0], unstaged_status=code[1], conflicted=code == "UU")
                elif entry.startswith("2 "):
                    fields = entry.split(" ", 9)
                    if len(fields) != 10 or len(fields[1]) != 2 or index >= len(chunks) - 1:
                        raise GitOutputError("Git returned malformed rename/copy status.")
                    code, path = fields[1], self._safe_reported_path(fields[9])
                    original = self._safe_reported_path(chunks[index])
                    index += 1
                    record = GitFileStatus(path=path, original_path=original, staged_status=code[0], unstaged_status=code[1], conflicted=code == "UU")
                else:
                    fields = entry.split(" ", 10)
                    if len(fields) != 11 or len(fields[1]) != 2:
                        raise GitOutputError("Git returned malformed conflict status.")
                    code, path = fields[1], self._safe_reported_path(fields[10])
                    record = GitFileStatus(path=path, staged_status=code[0], unstaged_status=code[1], conflicted=True)
                if record.staged_status not in {".", "?"}:
                    staged += 1
                if record.unstaged_status not in {".", "?"}:
                    unstaged += 1
                if record.conflicted:
                    conflicted += 1
            else:
                raise GitOutputError("Git returned an unknown porcelain status record.")
            if len(files) >= MAX_GIT_STATUS_RECORDS:
                truncated = True
            else:
                files.append(record)
        self._cached_paths = tuple(item.path for item in files[:_MAX_CACHED_PATHS])
        return GitStatus(
            repository=GitRepositoryAvailability.AVAILABLE,
            git_available=True, git_configured=self.configured, branch=branch, detached=detached,
            unborn=unborn, head_oid=head_oid, ahead=ahead, behind=behind, files=tuple(files),
            staged_count=staged, unstaged_count=unstaged, untracked_count=untracked,
            conflicted_count=conflicted, incomplete=truncated, truncated=truncated,
        )

    def _parse_commit_stream(self, text: str) -> tuple[GitCommit, ...]:
        text = self._require_clean_protocol_text(text)
        if not text:
            return ()
        fields = text.split("\0")
        if fields[-1] != "" or (len(fields) - 1) % 5:
            raise GitOutputError("Git returned malformed commit metadata.")
        commits: list[GitCommit] = []
        for index in range(0, len(fields) - 1, 5):
            oid, parents, author, timestamp, subject = fields[index:index + 5]
            if not _OID.fullmatch(oid):
                raise GitOutputError("Git returned malformed commit metadata.")
            try:
                parsed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                parsed_time = parsed_time.astimezone(UTC)
            except (ValueError, AttributeError):
                raise GitOutputError("Git returned malformed commit timestamps.") from None
            parent_values = tuple(parents.split()) if parents else ()
            if any(_OID.fullmatch(parent) is None for parent in parent_values):
                raise GitOutputError("Git returned malformed parent metadata.")
            if len(commits) >= MAX_GIT_COMMITS:
                raise GitOutputError("Git returned more commits than the request permits.")
            commits.append(GitCommit(
                oid=oid, parent_oids=parent_values[:MAX_GIT_PARENTS],
                parents_truncated=len(parent_values) > MAX_GIT_PARENTS,
                author_name=self._clean_untrusted(author, fallback="unknown", maximum=512),
                subject=self._clean_untrusted(subject, fallback="(no subject)", maximum=1024), authored_at=parsed_time,
            ))
        return tuple(commits)

    def _parse_blame(self, text: str) -> tuple[GitBlameRange, ...]:
        text = self._require_clean_protocol_text(text)
        blocks: list[GitBlameRange] = []
        current: dict[str, object] | None = None
        for raw in text.splitlines():
            if not raw:
                continue
            header = re.fullmatch(r"([0-9a-f]{40}|[0-9a-f]{64}) (\d+) (\d+) (\d+)", raw)
            if header is not None:
                if current is not None:
                    blocks.append(self._blame_block(current))
                current = {
                    "oid": header.group(1), "final": int(header.group(3)), "count": int(header.group(4)),
                    "author": None, "time": None, "tz": None,
                }
                continue
            if current is None:
                raise GitOutputError("Git returned malformed blame metadata.")
            key, separator, value = raw.partition(" ")
            if not separator:
                raise GitOutputError("Git returned malformed blame metadata.")
            if key == "author":
                current["author"] = self._clean_untrusted(value, fallback="unknown", maximum=512)
            elif key == "author-time":
                if not re.fullmatch(r"-?\d+", value):
                    raise GitOutputError("Git returned malformed blame timestamp.")
                current["time"] = int(value)
            elif key == "author-tz":
                if not re.fullmatch(r"[+-]\d{4}", value):
                    raise GitOutputError("Git returned malformed blame timezone.")
                current["tz"] = value
        if current is not None:
            blocks.append(self._blame_block(current))
        if len(blocks) > MAX_GIT_BLAME_RANGES:
            raise GitOutputError("Git returned more blame ranges than the request permits.")
        return tuple(blocks)

    @staticmethod
    def _blame_block(values: Mapping[str, object]) -> GitBlameRange:
        oid, final, count = values.get("oid"), values.get("final"), values.get("count")
        author, seconds, raw_tz = values.get("author"), values.get("time"), values.get("tz")
        if not (
            isinstance(oid, str) and isinstance(final, int) and isinstance(count, int)
            and isinstance(author, str) and isinstance(seconds, int) and isinstance(raw_tz, str)
            and count > 0 and final > 0
        ):
            raise GitOutputError("Git returned incomplete blame metadata.")
        offset = int(raw_tz[1:3]) * 60 + int(raw_tz[3:])
        if raw_tz[0] == "-":
            offset = -offset
        try:
            timestamp = datetime.fromtimestamp(seconds, timezone(timedelta(minutes=offset)))
        except (OverflowError, OSError, ValueError):
            raise GitOutputError("Git returned an invalid blame timestamp.") from None
        return GitBlameRange(
            start_line=final, end_line=final + count - 1, oid=oid,
            author_name=author, authored_at=timestamp,
        )

    def _validate_paths(self, paths: Sequence[str] | None) -> tuple[str, ...]:
        if paths is None:
            return ()
        if isinstance(paths, (str, bytes)):
            raise GitRequestError("paths must be a bounded collection of workspace-relative paths.")
        try:
            values = tuple(paths)
        except TypeError as error:
            raise GitRequestError("paths must be a bounded collection of workspace-relative paths.") from error
        if len(values) > MAX_GIT_PATHS or any(not isinstance(path, str) or not path or len(path) > 4096 for path in values):
            raise GitRequestError("Git requests accept at most 64 bounded workspace-relative paths.")
        safe: list[str] = []
        for path in values:
            try:
                relative = self._workspace.validate_reported_path(path)
            except WorkspaceError as error:
                raise GitRequestError("Git paths must remain inside the configured workspace.") from error
            if relative == ".":
                raise GitRequestError("Git paths must name files, not the workspace root.")
            safe.append(relative)
        if len({os.path.normcase(value) for value in safe}) != len(safe):
            raise GitRequestError("Git paths must not contain duplicates.")
        return tuple(safe)

    def _safe_reported_path(self, value: str) -> str:
        if not isinstance(value, str) or not value:
            raise GitOutputError("Git returned a malformed workspace path.")
        try:
            relative = self._workspace.validate_reported_path(value)
        except WorkspaceError as error:
            raise GitOutputError("Git returned a path outside the configured workspace.") from error
        if relative == "." or len(relative) > 4096:
            raise GitOutputError("Git returned an invalid workspace path.")
        return relative

    @staticmethod
    def _validate_oid(value: str) -> str:
        if not isinstance(value, str) or _OID.fullmatch(value) is None:
            raise GitRequestError("commit_oid must be one full SHA-1 or SHA-256 hexadecimal object identifier.")
        return value

    @staticmethod
    def _validate_cursor(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", value):
            raise GitRequestError("cursor must be an opaque ForgeMCP cursor.")
        return value

    def _new_cursor(self, skip: int) -> str:
        token = secrets.token_urlsafe(18)
        while token in self._cursors:
            token = secrets.token_urlsafe(18)
        if len(self._cursors) >= _MAX_CURSOR_CACHE:
            del self._cursors[next(iter(self._cursors))]
        self._cursors[token] = _Cursor(skip=skip)
        return token

    @staticmethod
    def _require_clean_protocol_text(value: str) -> str:
        if not isinstance(value, str) or "\ufffd" in value:
            raise GitOutputError("Git returned malformed non-UTF-8 metadata.")
        return value

    @staticmethod
    def _clean_untrusted(value: str, *, fallback: str, maximum: int) -> str:
        cleaned = _CONTROL.sub("", value)[:maximum]
        return cleaned if cleaned else fallback

    def _unavailable(self, message: str, *, error: bool = False) -> GitStatus:
        return GitStatus(
            repository=GitRepositoryAvailability.ERROR if error else GitRepositoryAvailability.UNAVAILABLE,
            git_available=self._qualification is not None,
            git_configured=self.configured,
            staged_count=0, unstaged_count=0, untracked_count=0, conflicted_count=0,
            incomplete=error, truncated=False, error=message,
        )

    def _cache_status(self, status: GitStatus) -> None:
        self._cached_status = status
        self._cached_at = datetime.now(UTC)
        self._invalidated = False
