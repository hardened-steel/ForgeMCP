"""Expected, content-free errors raised by the Workspace service."""

from __future__ import annotations

from forgemcp.core.errors import ForgeMCPError


class WorkspaceError(ForgeMCPError):
    """Base class for safe Workspace operation errors."""

    code = "workspace_error"


class WorkspacePathError(WorkspaceError):
    """A path is invalid for the configured workspace."""

    code = "workspace_path_error"


class IgnoredWorkspacePathError(WorkspacePathError):
    """A path is excluded by the Workspace policy."""

    code = "workspace_path_ignored"


class SymlinkWorkspacePathError(WorkspacePathError):
    """A path component is a symlink and cannot be traversed."""

    code = "workspace_symlink_not_allowed"


class WorkspaceFileNotFoundError(WorkspaceError):
    """A requested workspace file or directory does not exist."""

    code = "workspace_file_not_found"


class WorkspaceNotFileError(WorkspaceError):
    """An operation requiring a regular file received another filesystem object."""

    code = "workspace_not_a_file"


class WorkspaceNotDirectoryError(WorkspaceError):
    """An operation requiring a directory received another filesystem object."""

    code = "workspace_not_a_directory"


class WorkspaceFileTooLargeError(WorkspaceError):
    """A text or patch input exceeds a configured size limit."""

    code = "workspace_file_too_large"


class WorkspaceEncodingError(WorkspaceError):
    """A file cannot be decoded as strict UTF-8."""

    code = "workspace_encoding_error"


class InvalidUnifiedPatchError(WorkspaceError):
    """The supplied patch is outside ForgeMCP's supported unified-diff subset."""

    code = "invalid_unified_patch"


class ExpectedSnapshotError(WorkspaceError):
    """Expected snapshot input is incomplete or does not identify its target."""

    code = "expected_snapshot_error"


class WorkspaceConcurrentModificationError(WorkspaceError):
    """A file changed while a consistent read or snapshot was being captured."""

    code = "workspace_concurrent_modification"


class PatchCommitError(WorkspaceError):
    """A staged patch could not be committed without risking a partial result."""

    code = "patch_commit_error"


class WorkspaceTextEditError(WorkspaceError):
    """A structured text-edit batch has an invalid coordinate or overlap."""

    code = "workspace_text_edit_error"
