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
    WorkspaceTextEditError,
)
from forgemcp.workspace.policy import WorkspacePolicy
from forgemcp.workspace.service import (
    ExpectedSnapshot,
    GeneratedWorkspaceDirectory,
    ValidatedExecutionPath,
    WorkspaceService,
    WorkspaceTextEdit,
)

__all__ = [
    "ExpectedSnapshot",
    "GeneratedWorkspaceDirectory",
    "ValidatedExecutionPath",
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
    "WorkspaceTextEdit",
    "WorkspaceTextEditError",
    "WorkspacePolicy",
    "WorkspaceService",
]
