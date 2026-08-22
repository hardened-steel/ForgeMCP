"""Safe, transport-neutral filesystem operations for one ForgeMCP workspace."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TypeAlias

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import StructuredLogger
from forgemcp.models import FileChange, FileChangeKind, FileSnapshot, PatchResult, Position, Range
from forgemcp.workspace.errors import (
    ExpectedSnapshotError,
    IgnoredWorkspacePathError,
    InvalidUnifiedPatchError,
    PatchCommitError,
    SymlinkWorkspacePathError,
    WorkspaceEncodingError,
    WorkspaceConcurrentModificationError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    WorkspaceNotDirectoryError,
    WorkspaceNotFileError,
    WorkspacePathError,
    WorkspaceTextEditError,
)
from forgemcp.workspace.policy import WorkspacePolicy
from forgemcp.workspace.events import WorkspaceMutationBus


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?: .*)?$"
)
_PATCH_METADATA_PREFIXES = ("diff --git ", "index ", "new file mode ", "deleted file mode ")
_WINDOWS_RESERVED_COMPONENT = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$", re.IGNORECASE
)
MAX_WORKSPACE_LIST_FILES = 1_000
MAX_WORKSPACE_MUTATION_EDITS = 1_000

ExpectedSnapshot: TypeAlias = FileSnapshot | str | None


def _is_link_or_reparse_point(path: Path) -> bool:
    """Return whether a path is a symlink or Windows reparse point without traversal."""
    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


@dataclass(frozen=True, slots=True)
class WorkspaceTextEdit:
    """One replacement in public zero-based Unicode code-point coordinates.

    This intentionally lives in the Workspace package rather than shared
    domain models because ``new_text`` can contain source content and must not
    be treated as log-safe structured data.
    """

    range: Range
    new_text: str


@dataclass(frozen=True, slots=True)
class _PatchLine:
    """One parsed hunk line, without file content in logs or errors."""

    kind: str
    text: str
    ends_with_newline: bool


@dataclass(frozen=True, slots=True)
class _Hunk:
    """One validated unified-diff hunk."""

    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[_PatchLine, ...]


@dataclass(frozen=True, slots=True)
class _FilePatch:
    """A non-renaming create, modify, or delete patch for one relative path."""

    old_path: str | None
    new_path: str | None
    hunks: tuple[_Hunk, ...]

    @property
    def target_path(self) -> str:
        """Return the path whose expected snapshot guards this change."""
        return self.new_path if self.new_path is not None else self.old_path  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class _PlannedChange:
    """An entirely in-memory desired filesystem transition."""

    target: Path
    before: FileSnapshot
    kind: FileChangeKind
    after_text: str | None
    no_op: bool = False


@dataclass(slots=True)
class _StagedChange:
    """One staged replacement and, after commit starts, its rollback backup."""

    plan: _PlannedChange
    temporary_path: Path | None
    backup_path: Path | None = None
    after_snapshot: FileSnapshot | None = None


@dataclass(frozen=True, slots=True)
class GeneratedWorkspaceDirectory:
    """Capability for one explicit, non-symlink generated workspace directory.

    It deliberately exposes only the small set of generated-tree operations
    needed by integrations such as CMake's File API.  It never exposes a
    ``Path`` for a caller to use outside the Workspace boundary.
    """

    _service: "WorkspaceService"
    relative_path: str

    def write_text(self, path: str, text: str) -> None:
        """Atomically write a UTF-8 generated file below this directory."""
        self._service._write_generated_text(self.relative_path, path, text)

    def read_text(self, path: str) -> str:
        """Read one bounded UTF-8 generated file below this directory."""
        return self._service._read_generated_text(self.relative_path, path)

    def read_text_with_snapshot(self, path: str, *, maximum_bytes: int) -> tuple[str, FileSnapshot]:
        """Read bounded generated metadata and the snapshot of those exact bytes."""
        return self._service._read_generated_text_with_snapshot(
            self.relative_path, path, maximum_bytes=maximum_bytes
        )

    def is_empty(self) -> bool:
        """Return whether the generated directory contains no entries."""
        return self._service._generated_directory_is_empty(self.relative_path)

    def list_files(self, path: str = ".") -> tuple[str, ...]:
        """List direct regular, non-symlink file names below one generated directory."""
        return self._service._list_generated_files(self.relative_path, path)

    def get_snapshot(self, path: str) -> FileSnapshot:
        """Return metadata for one generated file without exposing its contents."""
        return self._service._get_generated_snapshot(self.relative_path, path)


@dataclass(frozen=True, slots=True)
class ValidatedExecutionPath:
    """A checked execution location without exposing a public ``pathlib.Path``.

    The capability has no read or write operations.  It deliberately permits
    generated build directories that ordinary workspace listing ignores.
    Replacement between validation and adapter launch remains a documented
    residual OS race.
    """

    relative_path: str
    native_path: str
    kind: str


class WorkspaceService:
    """Safely inspect and patch regular UTF-8 files below one workspace root.

    Public methods use workspace-relative strings and return only existing
    transport-neutral file models.  Text itself is returned only by
    :meth:`read_text`, and is deliberately never attached to a log record.
    """

    def __init__(
        self,
        config: ForgeConfig,
        logger: StructuredLogger,
        *,
        policy: WorkspacePolicy | None = None,
        mutations: WorkspaceMutationBus | None = None,
    ) -> None:
        """Bind the service to validated configuration and an explicit policy."""
        self._root = config.workspace_root
        self._logger = logger
        self._policy = WorkspacePolicy() if policy is None else policy
        self._mutations = mutations
        self._mutation_operation = 0
        self._mutation_lock = threading.RLock()
        self._filesystem_lock = threading.RLock()

    @property
    def workspace_root(self) -> Path:
        """Return the resolved workspace root without inspecting file contents."""
        return self._root

    @property
    def policy(self) -> WorkspacePolicy:
        """Return the immutable policy active for this service."""
        return self._policy

    def list_files(self, path: str = ".", recursive: bool = False) -> tuple[FileSnapshot, ...]:
        """List regular, non-symlink files below a workspace-relative directory.

        Excluded directories and all symlinks are omitted.  Asking to list a
        symlink or an excluded directory raises a domain error rather than
        traversing it.
        """
        directory = self._resolve_path(path)
        if not directory.exists():
            raise WorkspaceFileNotFoundError("The requested workspace directory does not exist.")
        if not directory.is_dir():
            raise WorkspaceNotDirectoryError("The requested workspace path is not a directory.")

        snapshots: list[FileSnapshot] = []
        self._collect_files(directory, recursive, snapshots)
        return tuple(snapshots)

    def read_text(self, path: str) -> tuple[str, FileSnapshot]:
        """Read one regular UTF-8 file and a snapshot matching the returned text."""
        target = self._resolve_path(path)
        for _ in range(3):
            snapshot = self._snapshot_path(target)
            if not snapshot.exists:
                raise WorkspaceFileNotFoundError("The requested workspace file does not exist.")
            if snapshot.size_bytes is not None and snapshot.size_bytes > self._policy.max_read_bytes:
                raise WorkspaceFileTooLargeError("The requested file exceeds the configured read limit.")
            data = self._read_bytes_limited(target, self._policy.max_read_bytes)
            if hashlib.sha256(data).hexdigest() != snapshot.sha256:
                continue
            try:
                return data.decode("utf-8"), snapshot
            except UnicodeDecodeError as error:
                raise WorkspaceEncodingError("The requested file is not valid UTF-8.") from error
        raise WorkspaceConcurrentModificationError(
            "The file changed while it was being read; retry the operation."
        )

    def get_snapshot(self, path: str) -> FileSnapshot:
        """Capture content-free metadata and SHA-256 for a regular file or absence."""
        return self._snapshot_path(self._resolve_path(path))

    def require_directory(self, path: str = ".") -> str:
        """Validate and normalize an existing ordinary workspace directory.

        The returned path is always workspace-relative and can safely be passed
        to another capability that accepts only workspace-relative paths.  The
        ordinary Workspace ignore policy remains in force for this operation.
        """
        directory = self._resolve_path(path)
        if not directory.exists():
            raise WorkspaceFileNotFoundError("The requested workspace directory does not exist.")
        if not directory.is_dir():
            raise WorkspaceNotDirectoryError("The requested workspace path is not a directory.")
        return self._relative_key(directory) or "."

    def open_generated_directory(
        self, path: str, *, create: bool = False
    ) -> GeneratedWorkspaceDirectory:
        """Open one explicit generated build directory inside this workspace.

        Generated directories may match the normal Workspace ignore policy, but
        still receive the exact same lexical workspace-boundary and symlink
        checks.  ``create=True`` creates missing non-symlink ancestors below
        the workspace; it never creates or follows a path outside it.
        """
        directory = self._resolve_path(path, apply_ignore_policy=False)
        if create:
            self._create_directory_without_symlinks(directory)
        if not directory.exists():
            raise WorkspaceFileNotFoundError("The requested generated directory does not exist.")
        if _is_link_or_reparse_point(directory):
            raise SymlinkWorkspacePathError("Workspace paths must not traverse symlinks.")
        if not directory.is_dir():
            raise WorkspaceNotDirectoryError("The requested generated path is not a directory.")
        return GeneratedWorkspaceDirectory(self, self._relative_key(directory) or ".")

    def validate_generated_directory_path(self, path: str) -> str:
        """Validate a possibly-not-yet-created build path without writing it.

        This is intentionally narrower than ``open_generated_directory`` and
        lets status/profile resolution apply the same lexical, containment and
        symlink policy without creating a build tree as an observation effect.
        """
        directory = self._resolve_path(path, apply_ignore_policy=False)
        self._assert_no_symlink_components(directory)
        return self._relative_key(directory) or "."

    def validate_reported_path(self, path: str, *, relative_to: str = ".") -> str:
        """Validate an untrusted absolute or relative path reported by a tool.

        Unlike normal caller-supplied workspace paths, a tool may report an
        absolute path.  This method accepts it only when it resolves beneath
        the configured workspace and neither its lexical path nor resolved
        path crosses a symlink.  The safe result is a workspace-relative path.
        """
        base = self._resolve_path(relative_to, apply_ignore_policy=False)
        if not base.exists() or not base.is_dir():
            raise WorkspaceNotDirectoryError("The reported-path base must be an existing workspace directory.")
        if not isinstance(path, str) or not path or "\x00" in path:
            raise WorkspacePathError("Reported paths must be non-empty NUL-free strings.")
        native = Path(path)
        windows = PureWindowsPath(path)
        posix = PurePosixPath(path)
        if bool(windows.drive) and not windows.is_absolute():
            raise WorkspacePathError("Drive-relative reported paths are not allowed.")
        if windows.is_absolute() or posix.is_absolute() or native.is_absolute() or bool(native.anchor):
            candidate = native
        else:
            candidate = base / native
        candidate_parts = candidate.parts[1:] if candidate.anchor else candidate.parts
        self._reject_windows_special_components(candidate_parts)
        self._assert_no_symlink_components(candidate)
        try:
            resolved = candidate.resolve(strict=False)
            relative = resolved.relative_to(self._root)
        except (OSError, ValueError) as error:
            raise WorkspacePathError("The reported path is outside the configured workspace.") from error
        self._assert_no_symlink_components(self._root / relative)
        return relative.as_posix() or "."

    def validate_execution_path(self, path: str, *, kind: str) -> ValidatedExecutionPath:
        """Validate an existing workspace file or directory for debugger launch.

        This bypasses only the normal listing ignore policy, so a generated
        build tree can be launched while all lexical, containment, symlink, and
        Windows reparse-point restrictions remain in force.
        """
        if kind not in {"file", "directory"}:
            raise WorkspacePathError("Execution paths must request file or directory validation.")
        candidate = self._resolve_path(path, apply_ignore_policy=False)
        self._assert_no_symlink_components(candidate)
        if not candidate.exists():
            raise WorkspaceFileNotFoundError("The requested execution path does not exist.")
        if _is_link_or_reparse_point(candidate):
            raise SymlinkWorkspacePathError("Execution paths must not be links or reparse points.")
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(self._root)
        except (FileNotFoundError, OSError, ValueError) as error:
            raise WorkspacePathError("The requested execution path is not safely inside the workspace.") from error
        if kind == "file" and not resolved.is_file():
            raise WorkspaceNotFileError("The requested execution path is not a regular file.")
        if kind == "directory" and not resolved.is_dir():
            raise WorkspaceNotDirectoryError("The requested execution path is not a directory.")
        return ValidatedExecutionPath(
            relative_path=self._relative_key(resolved) or ".",
            native_path=str(resolved),
            kind=kind,
        )

    def apply_unified_patch(
        self,
        patch: str,
        expected_snapshots: Mapping[str, ExpectedSnapshot],
    ) -> PatchResult:
        """Serialize one guarded unified patch through commit and publication."""
        with self._mutation_lock:
            return self._apply_unified_patch_locked(patch, expected_snapshots)

    def _apply_unified_patch_locked(
        self,
        patch: str,
        expected_snapshots: Mapping[str, ExpectedSnapshot],
    ) -> PatchResult:
        """Apply a guarded unified patch or report an unchanged failed result.

        Every touched path must have an expected snapshot.  A matching
        :class:`FileSnapshot` is preferred; a lowercase SHA-256 string is
        accepted for existing files, and ``None`` expresses an expected absent
        target for file creation.  Snapshot conflicts and hunk mismatches
        return ``PatchResult(applied=False)`` without mutating any source file.
        Invalid input and inaccessible paths raise domain errors.
        """
        file_patches = self._parse_unified_patch(patch)
        expected_by_target = self._normalize_expected_snapshots(expected_snapshots)

        targets: dict[str, Path] = {}
        patches_by_target: dict[str, _FilePatch] = {}
        for file_patch in file_patches:
            target = self._resolve_path(file_patch.target_path)
            key = self._path_identity(target)
            if key in targets:
                raise InvalidUnifiedPatchError("A unified patch may touch each file at most once.")
            targets[key] = target
            patches_by_target[key] = file_patch
        if set(targets) != set(expected_by_target):
            raise ExpectedSnapshotError("Expected snapshots must cover exactly the files in the patch.")

        plans: list[_PlannedChange] = []
        total_output_bytes = 0
        for key in sorted(targets):
            file_patch = patches_by_target[key]
            target = targets[key]
            current = self._snapshot_path(target)
            expected = expected_by_target[key]
            if not self._matches_expected_snapshot(target, current, expected):
                self._logger.warning("workspace_patch_not_applied", reason="snapshot_conflict")
                return PatchResult(applied=False)
            planned = self._plan_file_change(file_patch, target, current)
            if planned is None:
                self._logger.warning("workspace_patch_not_applied", reason="hunk_mismatch")
                return PatchResult(applied=False)
            total_output_bytes = self._add_bounded_mutation_output(
                total_output_bytes, planned.after_text
            )
            plans.append(planned)

        actionable = [plan for plan in plans if not plan.no_op]
        if not actionable:
            return PatchResult(applied=True)
        staged = self._stage_changes(actionable)
        try:
            if not self._commit_staged_changes(staged):
                self._logger.warning("workspace_patch_not_applied", reason="snapshot_conflict")
                return PatchResult(applied=False)
        finally:
            self._cleanup_staging(staged)

        changes = tuple(self._to_file_change(item) for item in staged)
        self._publish_mutations(actionable, changes)
        self._logger.info("workspace_patch_applied", changed_files=len(changes))
        return PatchResult(applied=True, changes=changes)

    def apply_text_edits(
        self,
        edits_by_path: Mapping[str, Sequence[WorkspaceTextEdit]],
        expected_snapshots: Mapping[str, ExpectedSnapshot],
    ) -> PatchResult:
        """Serialize one guarded text-edit batch through commit and publication."""
        with self._mutation_lock:
            return self._apply_text_edits_locked(edits_by_path, expected_snapshots)

    def _apply_text_edits_locked(
        self,
        edits_by_path: Mapping[str, Sequence[WorkspaceTextEdit]],
        expected_snapshots: Mapping[str, ExpectedSnapshot],
    ) -> PatchResult:
        """Atomically apply non-overlapping source-coordinate replacements.

        Every target must be an existing, ordinary workspace file with an
        expected snapshot.  Ranges use Unicode code points, matching the
        public location contract.  The entire batch is planned in memory and
        committed through the same staged rollback mechanism as unified
        patches, so any validation or compare-and-swap conflict leaves every
        file unchanged.  Replacement text is never logged.
        """
        if not isinstance(edits_by_path, Mapping):
            raise WorkspaceTextEditError("Text edits must be a mapping by workspace-relative path.")
        expected_by_target = self._normalize_expected_snapshots(expected_snapshots)
        targets: dict[str, tuple[Path, tuple[WorkspaceTextEdit, ...]]] = {}
        for supplied_path, supplied_edits in edits_by_path.items():
            if not isinstance(supplied_path, str):
                raise WorkspaceTextEditError("Text-edit paths must be workspace-relative strings.")
            target = self._resolve_path(supplied_path)
            key = self._path_identity(target)
            if key in targets:
                raise WorkspaceTextEditError("Text edits must not name a file more than once.")
            if isinstance(supplied_edits, (str, bytes)):
                raise WorkspaceTextEditError("Text edits for a file must be a sequence of structured edits.")
            try:
                edits = tuple(supplied_edits)
            except TypeError as error:
                raise WorkspaceTextEditError(
                    "Text edits for a file must be a sequence of structured edits."
                ) from error
            if not edits or any(not isinstance(edit, WorkspaceTextEdit) for edit in edits):
                raise WorkspaceTextEditError("Every target file must contain one or more structured text edits.")
            targets[key] = (target, edits)
        if set(targets) != set(expected_by_target):
            raise ExpectedSnapshotError("Expected snapshots must cover exactly the files in the text-edit batch.")
        if sum(len(edits) for _, edits in targets.values()) > MAX_WORKSPACE_MUTATION_EDITS:
            raise WorkspaceTextEditError("The text-edit batch exceeds the configured edit collection limit.")
        total_replacement_bytes = 0
        for _, edits in targets.values():
            for edit in edits:
                if not isinstance(edit.new_text, str) or "\x00" in edit.new_text:
                    raise WorkspaceTextEditError("Text-edit replacements must be NUL-free UTF-8 text.")
                try:
                    total_replacement_bytes += len(edit.new_text.encode("utf-8"))
                except UnicodeEncodeError as error:
                    raise WorkspaceTextEditError("Text-edit replacements must be valid UTF-8 text.") from error
                if total_replacement_bytes > self._policy.max_patch_bytes:
                    raise WorkspaceFileTooLargeError(
                        "Text-edit replacement data exceeds the configured batch size limit."
                    )

        plans: list[_PlannedChange] = []
        total_output_bytes = 0
        for key in sorted(targets):
            target, edits = targets[key]
            current = self._snapshot_path(target)
            expected = expected_by_target[key]
            if not self._matches_expected_snapshot(target, current, expected):
                self._logger.warning("workspace_text_edits_not_applied", reason="snapshot_conflict")
                return PatchResult(applied=False)
            if not current.exists:
                raise WorkspaceTextEditError("Text edits can only modify existing workspace files.")
            source_text = self._read_text_for_snapshot(target, current)
            replacement = self._apply_text_replacements(source_text, edits)
            if replacement != source_text:
                plan = _PlannedChange(
                    target=target,
                    before=current,
                    kind=FileChangeKind.MODIFIED,
                    after_text=replacement,
                )
                total_output_bytes = self._add_bounded_mutation_output(
                    total_output_bytes, plan.after_text
                )
                plans.append(plan)

        if not plans:
            return PatchResult(applied=True)
        staged = self._stage_changes(plans)
        try:
            if not self._commit_staged_changes(staged):
                self._logger.warning("workspace_text_edits_not_applied", reason="snapshot_conflict")
                return PatchResult(applied=False)
        finally:
            self._cleanup_staging(staged)
        changes = tuple(self._to_file_change(item) for item in staged)
        self._publish_mutations(plans, changes)
        self._logger.info("workspace_text_edits_applied", changed_files=len(changes))
        return PatchResult(applied=True, changes=changes)

    def _collect_files(self, directory: Path, recursive: bool, snapshots: list[FileSnapshot]) -> None:
        """Append a stable-order, non-following directory walk to ``snapshots``."""
        with os.scandir(directory) as entries:
            ordered_entries = sorted(entries, key=lambda entry: entry.name)
        for entry in ordered_entries:
            entry_path = Path(entry.path)
            if _is_link_or_reparse_point(entry_path):
                continue
            if entry.is_dir(follow_symlinks=False):
                if self._policy.ignores_directory(entry.name):
                    continue
                if recursive:
                    self._collect_files(entry_path, True, snapshots)
            elif entry.is_file(follow_symlinks=False):
                if len(snapshots) >= MAX_WORKSPACE_LIST_FILES:
                    raise WorkspaceFileTooLargeError(
                        "The workspace file listing exceeds the configured collection limit."
                    )
                snapshots.append(self._snapshot_path(entry_path))

    def _resolve_path(self, path: str, *, apply_ignore_policy: bool = True) -> Path:
        """Validate a relative path lexically and reject every symlink component."""
        if not isinstance(path, str) or not path or "\x00" in path:
            raise WorkspacePathError("Workspace paths must be non-empty relative strings.")
        native_path = Path(path)
        windows_path = PureWindowsPath(path)
        posix_path = PurePosixPath(path)
        if (
            native_path.is_absolute()
            or bool(native_path.anchor)
            or bool(windows_path.drive)
            or bool(windows_path.root)
            or windows_path.is_absolute()
            or posix_path.is_absolute()
        ):
            raise WorkspacePathError("Absolute workspace paths are not allowed.")
        parts = tuple(part for part in native_path.parts if part not in {".", ""})
        if any(part == ".." for part in parts):
            raise WorkspacePathError("Workspace paths must not contain parent traversal.")
        self._reject_windows_special_components(parts)

        candidate = self._root
        for index, part in enumerate(parts):
            candidate = candidate / part
            if _is_link_or_reparse_point(candidate):
                raise SymlinkWorkspacePathError("Workspace paths must not traverse symlinks.")
            if apply_ignore_policy and index < len(parts) - 1 and self._policy.ignores_directory(part):
                raise IgnoredWorkspacePathError("The requested path is excluded by workspace policy.")
        if (
            apply_ignore_policy
            and candidate != self._root
            and candidate.is_dir()
            and self._policy.ignores_directory(candidate.name)
        ):
            raise IgnoredWorkspacePathError("The requested path is excluded by workspace policy.")
        return candidate

    def _create_directory_without_symlinks(self, directory: Path) -> None:
        """Create a generated directory while rejecting every existing symlink component."""
        relative = directory.relative_to(self._root)
        candidate = self._root
        for part in relative.parts:
            candidate = candidate / part
            if _is_link_or_reparse_point(candidate):
                raise SymlinkWorkspacePathError("Workspace paths must not traverse symlinks.")
            if not candidate.exists():
                try:
                    candidate.mkdir()
                except FileExistsError:
                    # A concurrent creator may have installed a symlink or a file.
                    if _is_link_or_reparse_point(candidate):
                        raise SymlinkWorkspacePathError("Workspace paths must not traverse symlinks.")
                    if not candidate.is_dir():
                        raise WorkspaceNotDirectoryError(
                            "A generated-directory path component is not a directory."
                        )
            elif not candidate.is_dir():
                raise WorkspaceNotDirectoryError(
                    "A generated-directory path component is not a directory."
                )

    def _resolve_generated_child(self, directory: str, path: str) -> Path:
        """Resolve a relative child below a generated directory without policy bypasses."""
        root = self._resolve_path(directory, apply_ignore_policy=False)
        if not isinstance(path, str) or not path or "\x00" in path:
            raise WorkspacePathError("Generated-file paths must be non-empty relative strings.")
        native = Path(path)
        windows = PureWindowsPath(path)
        posix = PurePosixPath(path)
        if (
            native.is_absolute()
            or bool(native.anchor)
            or bool(windows.drive)
            or bool(windows.root)
            or windows.is_absolute()
            or posix.is_absolute()
        ):
            raise WorkspacePathError("Generated-file paths must be relative to their generated directory.")
        parts = tuple(part for part in native.parts if part not in {"", "."})
        if any(part == ".." for part in parts):
            raise WorkspacePathError("Generated-file paths must not contain parent traversal.")
        self._reject_windows_special_components(parts)
        candidate = root
        for part in parts:
            candidate = candidate / part
            if _is_link_or_reparse_point(candidate):
                raise SymlinkWorkspacePathError("Workspace paths must not traverse symlinks.")
        return candidate

    def _write_generated_text(self, directory: str, path: str, text: str) -> None:
        """Atomically replace one bounded UTF-8 generated file without logging content."""
        if not isinstance(text, str):
            raise WorkspaceEncodingError("Generated file contents must be UTF-8 text.")
        try:
            encoded = text.encode("utf-8")
        except UnicodeEncodeError as error:
            raise WorkspaceEncodingError("Generated file contents must be valid UTF-8 text.") from error
        if len(encoded) > self._policy.max_patch_bytes:
            raise WorkspaceFileTooLargeError("Generated file contents exceed the configured size limit.")
        target = self._resolve_generated_child(directory, path)
        self._create_directory_without_symlinks(target.parent)
        if _is_link_or_reparse_point(target):
            raise SymlinkWorkspacePathError("Workspace paths must not traverse symlinks.")
        descriptor, temporary_name = tempfile.mkstemp(prefix=".forgemcp-generated-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            if target.exists() and not target.is_file():
                raise WorkspaceNotFileError("The generated path is not a regular file.")
            os.replace(temporary, target)
        finally:
            if temporary.exists() and not _is_link_or_reparse_point(temporary):
                temporary.unlink()

    def _read_generated_text(self, directory: str, path: str) -> str:
        """Read one generated regular file using the standard Workspace limits."""
        target = self._resolve_generated_child(directory, path)
        snapshot = self._snapshot_path(target)
        if not snapshot.exists:
            raise WorkspaceFileNotFoundError("The requested generated file does not exist.")
        data = self._read_bytes_limited(target, self._policy.max_read_bytes)
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkspaceEncodingError("The requested generated file is not valid UTF-8.") from error

    def _read_generated_text_with_snapshot(
        self, directory: str, path: str, *, maximum_bytes: int
    ) -> tuple[str, FileSnapshot]:
        """Read bounded generated metadata while binding its digest to those bytes."""
        if not isinstance(maximum_bytes, int) or isinstance(maximum_bytes, bool) or maximum_bytes <= 0:
            raise WorkspaceFileTooLargeError("Generated file limit must be a positive byte count.")
        target = self._resolve_generated_child(directory, path)
        for _ in range(3):
            if _is_link_or_reparse_point(target):
                raise SymlinkWorkspacePathError("Workspace paths must not traverse symlinks.")
            if not target.exists():
                raise WorkspaceFileNotFoundError("The requested generated file does not exist.")
            if not target.is_file():
                raise WorkspaceNotFileError("The requested generated path is not a regular file.")
            before = target.stat()
            if before.st_size > maximum_bytes:
                raise WorkspaceFileTooLargeError("The requested generated file exceeds the configured read limit.")
            data = self._read_bytes_limited(target, maximum_bytes)
            after = target.stat()
            if (
                before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
            ):
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise WorkspaceEncodingError("The requested generated file is not valid UTF-8.") from error
            return (
                text,
                FileSnapshot(
                    uri=target.as_uri(),
                    exists=True,
                    size_bytes=after.st_size,
                    sha256=hashlib.sha256(data).hexdigest(),
                    modified_at=datetime.fromtimestamp(after.st_mtime, UTC),
                    captured_at=datetime.now(UTC),
                ),
            )
        raise WorkspaceConcurrentModificationError(
            "The generated file changed while it was being read; retry the operation."
        )

    def _generated_directory_is_empty(self, directory: str) -> bool:
        """Inspect entry names only; never traverse a generated build tree."""
        target = self._resolve_path(directory, apply_ignore_policy=False)
        if not target.exists() or not target.is_dir():
            raise WorkspaceNotDirectoryError("The requested generated path is not a directory.")
        with os.scandir(target) as entries:
            return next(entries, None) is None

    def _list_generated_files(self, directory: str, path: str) -> tuple[str, ...]:
        """List direct regular files in a generated directory without following links."""
        target = self._resolve_generated_child(directory, path)
        if not target.exists():
            raise WorkspaceFileNotFoundError("The requested generated directory does not exist.")
        if not target.is_dir():
            raise WorkspaceNotDirectoryError("The requested generated path is not a directory.")
        with os.scandir(target) as entries:
            return tuple(
                entry.name
                for entry in sorted(entries, key=lambda entry: entry.name)
                if not _is_link_or_reparse_point(Path(entry.path)) and entry.is_file(follow_symlinks=False)
            )

    def _get_generated_snapshot(self, directory: str, path: str) -> FileSnapshot:
        """Capture metadata for one generated regular file with normal boundary checks."""
        return self._snapshot_path(self._resolve_generated_child(directory, path))

    @staticmethod
    def _assert_no_symlink_components(candidate: Path) -> None:
        """Reject a lexical path that names any existing symlink component."""
        anchor = Path(candidate.anchor)
        current = anchor
        parts = candidate.parts[1:] if candidate.anchor else candidate.parts
        for part in parts:
            current = current / part
            if _is_link_or_reparse_point(current):
                raise SymlinkWorkspacePathError("Workspace paths must not traverse symlinks.")

    def _relative_key(self, target: Path) -> str:
        """Return the canonical forward-slash workspace-relative key for a safe path."""
        return target.relative_to(self._root).as_posix()

    def _path_identity(self, target: Path) -> str:
        """Return a platform-canonical key for duplicate detection and ordering."""
        return os.path.normcase(self._relative_key(target))

    def _snapshot_path(self, target: Path) -> FileSnapshot:
        """Snapshot an existing regular file, or represent an absent safe target."""
        if _is_link_or_reparse_point(target):
            raise SymlinkWorkspacePathError("Workspace paths must not traverse symlinks.")
        if not target.exists():
            return FileSnapshot(uri=target.as_uri(), exists=False, captured_at=datetime.now(UTC))
        if not target.is_file():
            raise WorkspaceNotFileError("The requested workspace path is not a regular file.")
        for _ in range(3):
            before = target.stat()
            digest = self._hash_file(target)
            after = target.stat()
            if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
                return FileSnapshot(
                    uri=target.as_uri(),
                    exists=True,
                    size_bytes=after.st_size,
                    sha256=digest,
                    modified_at=datetime.fromtimestamp(after.st_mtime, UTC),
                    captured_at=datetime.now(UTC),
                )
        raise WorkspaceConcurrentModificationError(
            "The file changed while its snapshot was being captured; retry the operation."
        )

    @staticmethod
    def _hash_file(target: Path) -> str:
        """Stream a SHA-256 without retaining file contents in service state."""
        digest = hashlib.sha256()
        with target.open("rb") as source:
            for chunk in iter(lambda: source.read(65_536), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _read_bytes_limited(self, target: Path, maximum: int) -> bytes:
        """Read at most one configured byte limit plus a sentinel byte."""
        if _is_link_or_reparse_point(target):
            raise SymlinkWorkspacePathError("Workspace paths must not traverse symlinks.")
        with target.open("rb") as source:
            data = source.read(maximum + 1)
        if len(data) > maximum:
            raise WorkspaceFileTooLargeError("The requested file exceeds the configured read limit.")
        return data

    def _read_text_for_snapshot(self, target: Path, snapshot: FileSnapshot) -> str:
        """Read source text only when it still matches a prevalidated snapshot."""
        data = self._read_bytes_limited(target, self._policy.max_read_bytes)
        if hashlib.sha256(data).hexdigest() != snapshot.sha256:
            raise WorkspaceConcurrentModificationError(
                "The file changed while text edits were being planned; retry the operation."
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkspaceEncodingError("The requested file is not valid UTF-8.") from error

    def _apply_text_replacements(
        self, source_text: str, edits: tuple[WorkspaceTextEdit, ...]
    ) -> str:
        """Validate, order, and apply one non-overlapping batch without logging text."""
        indexed: list[tuple[int, int, str]] = []
        total_replacement_bytes = 0
        for edit in edits:
            if not isinstance(edit.new_text, str) or "\x00" in edit.new_text:
                raise WorkspaceTextEditError("Text-edit replacements must be NUL-free UTF-8 text.")
            try:
                total_replacement_bytes += len(edit.new_text.encode("utf-8"))
            except UnicodeEncodeError as error:
                raise WorkspaceTextEditError("Text-edit replacements must be valid UTF-8 text.") from error
            start = self._position_offset(source_text, edit.range.start)
            end = self._position_offset(source_text, edit.range.end)
            if end < start:
                raise WorkspaceTextEditError("A text-edit range must not run backwards.")
            indexed.append((start, end, edit.new_text))
        if total_replacement_bytes > self._policy.max_patch_bytes:
            raise WorkspaceFileTooLargeError("Text-edit replacement data exceeds the configured size limit.")
        ordered = sorted(indexed, key=lambda item: (item[0], item[1], item[2]))
        previous_start = -1
        previous_end = -1
        for start, end, _ in ordered:
            if start < previous_end or (start == previous_start and end == previous_end):
                raise WorkspaceTextEditError("Text edits for one file must not overlap.")
            previous_start, previous_end = start, end
        output = source_text
        for start, end, replacement in reversed(ordered):
            output = output[:start] + replacement + output[end:]
        try:
            if len(output.encode("utf-8")) > self._policy.max_patch_bytes:
                raise WorkspaceFileTooLargeError("Text-edit output exceeds the configured size limit.")
        except UnicodeEncodeError as error:
            raise WorkspaceTextEditError("Text-edit output must be valid UTF-8 text.") from error
        return output

    @staticmethod
    def _position_offset(source_text: str, position: Position) -> int:
        """Translate a code-point line/column into a Python string offset."""
        raw_lines = source_text.splitlines(keepends=True)
        if not raw_lines:
            raw_lines = [""]
        elif source_text.endswith(("\n", "\r")):
            raw_lines.append("")
        if position.line >= len(raw_lines):
            raise WorkspaceTextEditError("A text-edit line is outside the current document.")
        line = raw_lines[position.line]
        if line.endswith("\r\n"):
            visible = line[:-2]
        elif line.endswith(("\n", "\r")):
            visible = line[:-1]
        else:
            visible = line
        if position.column > len(visible):
            raise WorkspaceTextEditError("A text-edit column is outside the current document line.")
        return sum(len(item) for item in raw_lines[: position.line]) + position.column

    def _normalize_expected_snapshots(
        self, expected_snapshots: Mapping[str, ExpectedSnapshot]
    ) -> dict[str, ExpectedSnapshot]:
        """Canonicalize expected-snapshot keys before any file content is read."""
        if not isinstance(expected_snapshots, Mapping):
            raise ExpectedSnapshotError("Expected snapshots must be a mapping by workspace-relative path.")
        normalized: dict[str, ExpectedSnapshot] = {}
        for supplied_path, expected in expected_snapshots.items():
            if not isinstance(supplied_path, str):
                raise ExpectedSnapshotError("Expected snapshot paths must be strings.")
            target = self._resolve_path(supplied_path)
            key = self._path_identity(target)
            if key in normalized:
                raise ExpectedSnapshotError("Expected snapshots must not name a file more than once.")
            if not isinstance(expected, (FileSnapshot, str)) and expected is not None:
                raise ExpectedSnapshotError("Expected snapshots must be FileSnapshot, SHA-256, or None.")
            normalized[key] = expected
        return normalized

    def _matches_expected_snapshot(
        self, target: Path, current: FileSnapshot, expected: ExpectedSnapshot
    ) -> bool:
        """Check optimistic-concurrency input without accepting an ambiguous digest."""
        if isinstance(expected, FileSnapshot):
            if expected.uri != target.as_uri():
                raise ExpectedSnapshotError("An expected FileSnapshot belongs to a different workspace path.")
            if expected.exists and expected.sha256 is None:
                raise ExpectedSnapshotError("Existing expected snapshots require a SHA-256 digest.")
            return expected.exists == current.exists and expected.sha256 == current.sha256
        if isinstance(expected, str):
            if not _SHA256_HEX.fullmatch(expected):
                raise ExpectedSnapshotError("Expected SHA-256 values must be 64 lowercase hexadecimal characters.")
            return current.exists and current.sha256 == expected
        return not current.exists

    @staticmethod
    def _same_snapshot(left: FileSnapshot, right: FileSnapshot) -> bool:
        """Compare the state relevant to safe compare-and-swap commits."""
        return left.exists == right.exists and left.sha256 == right.sha256

    def _plan_file_change(
        self, file_patch: _FilePatch, target: Path, before: FileSnapshot
    ) -> _PlannedChange | None:
        """Apply one parsed patch in memory and produce a desired file transition."""
        creating = file_patch.old_path is None
        deleting = file_patch.new_path is None
        if creating and before.exists:
            return None
        if not creating and not before.exists:
            return None
        if creating and not target.parent.is_dir():
            raise WorkspaceFileNotFoundError("Patch target parent directory does not exist.")
        if deleting:
            source_text = self._read_patch_text(target, before)
            new_text = self._apply_hunks(source_text, file_patch.hunks)
            if new_text is None or new_text:
                return None
            return _PlannedChange(target=target, before=before, kind=FileChangeKind.DELETED, after_text=None)

        source_text = "" if creating else self._read_patch_text(target, before)
        new_text = self._apply_hunks(source_text, file_patch.hunks)
        if new_text is None:
            return None
        kind = FileChangeKind.CREATED if creating else FileChangeKind.MODIFIED
        return _PlannedChange(
            target=target,
            before=before,
            kind=kind,
            after_text=new_text,
            no_op=not creating and new_text == source_text,
        )

    def _read_patch_text(self, target: Path, snapshot: FileSnapshot) -> str:
        """Read a bounded UTF-8 patch source and ensure it still matches its snapshot."""
        if snapshot.size_bytes is not None and snapshot.size_bytes > self._policy.max_read_bytes:
            raise WorkspaceFileTooLargeError("The patch source exceeds the configured read limit.")
        data = self._read_bytes_limited(target, self._policy.max_read_bytes)
        if hashlib.sha256(data).hexdigest() != snapshot.sha256:
            raise WorkspaceConcurrentModificationError(
                "The file changed while the patch source was being read; retry the operation."
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WorkspaceEncodingError("The patch source is not valid UTF-8.") from error

    def _add_bounded_mutation_output(self, total: int, text: str | None) -> int:
        """Cap aggregate staged UTF-8 data before the first filesystem write."""
        if text is None:
            return total
        try:
            next_total = total + len(text.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise WorkspaceEncodingError("Workspace mutation output must be valid UTF-8 text.") from error
        if next_total > self._policy.max_patch_bytes:
            raise WorkspaceFileTooLargeError(
                "The workspace mutation exceeds the configured aggregate output limit."
            )
        return next_total

    def _stage_changes(self, plans: list[_PlannedChange]) -> list[_StagedChange]:
        """Write all desired files beside their targets before changing any target."""
        staged: list[_StagedChange] = []
        try:
            for plan in plans:
                current = _StagedChange(plan=plan, temporary_path=None)
                staged.append(current)
                if current.plan.after_text is not None:
                    descriptor, temporary_name = tempfile.mkstemp(
                        prefix=".forgemcp-", suffix=".tmp", dir=plan.target.parent
                    )
                    current.temporary_path = Path(temporary_name)
                    with os.fdopen(descriptor, "wb") as temporary_file:
                        temporary_file.write(current.plan.after_text.encode("utf-8"))
                        temporary_file.flush()
                        os.fsync(temporary_file.fileno())
            return staged
        except OSError as error:
            self._cleanup_staging(staged)
            raise PatchCommitError("Patch staging failed before any source file changed.") from error

    def _commit_staged_changes(self, staged: list[_StagedChange]) -> bool:
        """Replace every target, restoring earlier targets if a later replace fails."""
        with self._filesystem_lock:
            # The final compare-and-swap happens under the application-local
            # filesystem lock.  A cross-process writer remains a documented
            # OS race, but concurrent ForgeMCP requests cannot interleave the
            # check and replacement boundary.
            for item in staged:
                current = self._snapshot_path(item.plan.target)
                if not self._same_snapshot(current, item.plan.before):
                    return False
            committed: list[_StagedChange] = []
            try:
                for item in staged:
                    plan = item.plan
                    committed.append(item)
                    if plan.before.exists:
                        descriptor, backup_name = tempfile.mkstemp(
                            prefix=".forgemcp-", suffix=".backup", dir=plan.target.parent
                        )
                        os.close(descriptor)
                        item.backup_path = Path(backup_name)
                        os.replace(plan.target, item.backup_path)
                    if item.temporary_path is not None:
                        os.replace(item.temporary_path, plan.target)
                        item.temporary_path = None
                    if plan.kind is not FileChangeKind.DELETED:
                        item.after_snapshot = self._snapshot_path(plan.target)
            except OSError as error:
                self._restore_committed_changes(committed)
                raise PatchCommitError("Patch commit failed; ForgeMCP restored the previous file state.") from error
        return True

    def _restore_committed_changes(self, committed: list[_StagedChange]) -> None:
        """Best-effort rollback for targets already replaced during this operation."""
        rollback_failed = False
        for item in reversed(committed):
            try:
                if item.backup_path is not None and item.backup_path.exists():
                    os.replace(item.backup_path, item.plan.target)
                    item.backup_path = None
                elif (
                    item.plan.kind is FileChangeKind.CREATED
                    and item.temporary_path is None
                    and item.plan.target.exists()
                ):
                    item.plan.target.unlink()
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise PatchCommitError("Patch commit failed and automatic rollback could not complete safely.")

    def _cleanup_staging(self, staged: list[_StagedChange]) -> None:
        """Remove only ForgeMCP-created temporary and backup files."""
        for item in staged:
            for path in (item.temporary_path, item.backup_path):
                if path is None:
                    continue
                try:
                    if path.exists() and not _is_link_or_reparse_point(path):
                        path.unlink()
                except OSError:
                    self._logger.warning("workspace_temporary_cleanup_failed")

    def _to_file_change(self, item: _StagedChange) -> FileChange:
        """Build a content-free success report after the staged commit completes."""
        plan = item.plan
        after = None if plan.kind is FileChangeKind.DELETED else item.after_snapshot
        before = None if plan.kind is FileChangeKind.CREATED else plan.before
        return FileChange(uri=plan.target.as_uri(), kind=plan.kind, before=before, after=after)

    def _publish_mutations(
        self, plans: Sequence[_PlannedChange], changes: Sequence[FileChange]
    ) -> None:
        """Queue one ordered post-commit batch after staging cleanup is complete."""
        if self._mutations is None or not changes:
            return
        with self._filesystem_lock:
            self._mutation_operation += 1
            operation_id = f"workspace-{self._mutation_operation}"
        event_changes: list[tuple[str, FileChangeKind, FileSnapshot | None, FileSnapshot | None]] = []
        for plan, change in zip(plans, changes, strict=True):
            event_changes.append(
                (self._relative_key(plan.target), change.kind, change.before, change.after)
            )
        # Publication never invokes a subscriber while Workspace has staging
        # state. Failure or saturation is contained by the application bus.
        self._mutations.publish(tuple(event_changes), operation_id=operation_id)

    @staticmethod
    def _reject_windows_special_components(parts: Sequence[str]) -> None:
        """Deny ADS and reserved device spellings on every platform."""
        for part in parts:
            if ":" in part or part.rstrip(" .") != part or _WINDOWS_RESERVED_COMPONENT.fullmatch(part):
                raise WorkspacePathError(
                    "Workspace paths must not use Windows device or alternate-data-stream names."
                )

    def _parse_unified_patch(self, patch: str) -> tuple[_FilePatch, ...]:
        """Parse the deliberately small, text-only unified-diff subset we support."""
        if not isinstance(patch, str):
            raise InvalidUnifiedPatchError("Unified patches must be UTF-8 text strings.")
        try:
            patch_bytes = patch.encode("utf-8")
        except UnicodeEncodeError as error:
            raise InvalidUnifiedPatchError("Unified patches must be valid UTF-8 text.") from error
        if len(patch_bytes) > self._policy.max_patch_bytes:
            raise WorkspaceFileTooLargeError("The supplied patch exceeds the configured patch limit.")
        lines = patch.splitlines(keepends=True)
        if not lines:
            raise InvalidUnifiedPatchError("A unified patch must contain at least one file change.")

        parsed: list[_FilePatch] = []
        index = 0
        while index < len(lines):
            line = self._strip_line_ending(lines[index])
            if line.startswith(_PATCH_METADATA_PREFIXES):
                index += 1
                continue
            if not line.startswith("--- "):
                raise InvalidUnifiedPatchError("Patch input must use unified-diff file headers.")
            old_path = self._parse_patch_header(line[4:], "a/")
            index += 1
            if index >= len(lines) or not self._strip_line_ending(lines[index]).startswith("+++ "):
                raise InvalidUnifiedPatchError("Each old file header must be followed by a new file header.")
            new_path = self._parse_patch_header(self._strip_line_ending(lines[index])[4:], "b/")
            if old_path is None and new_path is None:
                raise InvalidUnifiedPatchError("A patch cannot use /dev/null for both file headers.")
            if old_path is not None and new_path is not None and old_path != new_path:
                raise InvalidUnifiedPatchError("File renames are not supported by the workspace patch format.")
            index += 1

            hunks: list[_Hunk] = []
            while index < len(lines):
                line = self._strip_line_ending(lines[index])
                if line.startswith("--- ") or line.startswith(_PATCH_METADATA_PREFIXES):
                    break
                if not line.startswith("@@ "):
                    raise InvalidUnifiedPatchError("Unified patches may contain only hunks after file headers.")
                hunk, index = self._parse_hunk(lines, index)
                hunks.append(hunk)
            if not hunks:
                raise InvalidUnifiedPatchError("Each patched file must contain at least one hunk.")
            parsed.append(_FilePatch(old_path=old_path, new_path=new_path, hunks=tuple(hunks)))
        return tuple(parsed)

    def _parse_hunk(self, lines: list[str], index: int) -> tuple[_Hunk, int]:
        """Parse and count-check one hunk without retaining it outside this call."""
        match = _HUNK_HEADER.fullmatch(self._strip_line_ending(lines[index]))
        if match is None:
            raise InvalidUnifiedPatchError("A hunk header has invalid unified-diff coordinates.")
        old_start = int(match.group("old_start"))
        old_count = int(match.group("old_count") or "1")
        new_start = int(match.group("new_start"))
        new_count = int(match.group("new_count") or "1")
        if (old_start == 0 and old_count != 0) or (new_start == 0 and new_count != 0):
            raise InvalidUnifiedPatchError("Zero hunk coordinates are valid only for empty ranges.")
        index += 1
        hunk_lines: list[_PatchLine] = []
        while index < len(lines):
            raw_line = lines[index]
            line = self._strip_line_ending(raw_line)
            old_lines = sum(item.kind in {" ", "-"} for item in hunk_lines)
            new_lines = sum(item.kind in {" ", "+"} for item in hunk_lines)
            if line.startswith("@@ ") or (
                (line.startswith("--- ") or line.startswith(_PATCH_METADATA_PREFIXES))
                and old_lines == old_count
                and new_lines == new_count
            ):
                break
            if line == "\\ No newline at end of file":
                raise InvalidUnifiedPatchError("Patches without a final newline are not supported yet.")
            if not line or line[0] not in {" ", "+", "-"}:
                raise InvalidUnifiedPatchError("A hunk contains an invalid line prefix.")
            hunk_lines.append(
                _PatchLine(
                    kind=line[0],
                    text=line[1:],
                    ends_with_newline=raw_line.endswith(("\n", "\r")),
                )
            )
            index += 1
        if sum(line.kind in {" ", "-"} for line in hunk_lines) != old_count:
            raise InvalidUnifiedPatchError("A hunk's old range does not match its line count.")
        if sum(line.kind in {" ", "+"} for line in hunk_lines) != new_count:
            raise InvalidUnifiedPatchError("A hunk's new range does not match its line count.")
        return (
            _Hunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=tuple(hunk_lines),
            ),
            index,
        )

    @staticmethod
    def _strip_line_ending(line: str) -> str:
        """Remove only the line terminator, preserving all source text characters."""
        return line[:-2] if line.endswith("\r\n") else line[:-1] if line.endswith(("\n", "\r")) else line

    @staticmethod
    def _parse_patch_header(value: str, expected_prefix: str) -> str | None:
        """Extract a single text path from a standard unified-diff header."""
        raw_path = value.split("\t", 1)[0]
        if raw_path == "/dev/null":
            return None
        if not raw_path:
            raise InvalidUnifiedPatchError("Patch file headers must name one path or /dev/null.")
        return raw_path[len(expected_prefix) :] if raw_path.startswith(expected_prefix) else raw_path

    @staticmethod
    def _apply_hunks(source_text: str, hunks: tuple[_Hunk, ...]) -> str | None:
        """Apply coordinate-checked hunks to text in memory, returning None on mismatch."""
        source_lines = source_text.splitlines()
        line_ending = "\r\n" if "\r\n" in source_text else "\n"
        final_newline = source_text.endswith(("\n", "\r"))
        output: list[str] = []
        cursor = 0
        for hunk in hunks:
            position = hunk.old_start if hunk.old_count == 0 else hunk.old_start - 1
            if position < cursor or position > len(source_lines):
                return None
            output.extend(source_lines[cursor:position])
            cursor = position
            hunk_new_lines: list[_PatchLine] = []
            for line in hunk.lines:
                if line.kind in {" ", "-"}:
                    if cursor >= len(source_lines) or source_lines[cursor] != line.text:
                        return None
                    cursor += 1
                if line.kind in {" ", "+"}:
                    output.append(line.text)
                    hunk_new_lines.append(line)
            if cursor == len(source_lines) and hunk_new_lines:
                final_newline = hunk_new_lines[-1].ends_with_newline
        output.extend(source_lines[cursor:])
        if not output:
            return ""
        return line_ending.join(output) + (line_ending if final_newline else "")
