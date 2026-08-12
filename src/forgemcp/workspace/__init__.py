"""Safe filesystem access for the configured ForgeMCP workspace."""

from forgemcp.workspace.errors import (
    ExpectedSnapshotError,
    IgnoredWorkspacePathError,
    InvalidUnifiedPatchError,
    PatchCommitError,
    SymlinkWorkspacePathError,
    WorkspaceEncodingError,
    WorkspaceConcurrentModificationError,
    WorkspaceError,
    WorkspaceFileNotFoundError,
    WorkspaceFileTooLargeError,
    WorkspaceNotDirectoryError,
    WorkspaceNotFileError,
    WorkspacePathError,
)
from forgemcp.workspace.policy import WorkspacePolicy
from forgemcp.workspace.service import ExpectedSnapshot, GeneratedWorkspaceDirectory, WorkspaceService

__all__ = [
    "ExpectedSnapshot",
    "GeneratedWorkspaceDirectory",
    "ExpectedSnapshotError",
    "IgnoredWorkspacePathError",
    "InvalidUnifiedPatchError",
    "PatchCommitError",
    "SymlinkWorkspacePathError",
    "WorkspaceEncodingError",
    "WorkspaceConcurrentModificationError",
    "WorkspaceError",
    "WorkspaceFileNotFoundError",
    "WorkspaceFileTooLargeError",
    "WorkspaceNotDirectoryError",
    "WorkspaceNotFileError",
    "WorkspacePathError",
    "WorkspacePolicy",
    "WorkspaceService",
]
