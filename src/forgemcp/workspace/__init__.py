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
    WorkspaceRequestError,
    WorkspaceTextEditError,
)
from forgemcp.workspace.service import (
    ExpectedSnapshot,
    GeneratedWorkspaceDirectory,
    ValidatedExecutionPath,
    WorkspaceService,
    WorkspaceTextEdit,
)
from forgemcp.workspace.policy import WorkspacePolicy
from forgemcp.workspace.events import (
    WorkspaceMutation,
    WorkspaceMutationBatch,
    WorkspaceMutationBus,
    WorkspaceMutationSubscription,
)
from forgemcp.workspace.plugin import WorkspacePlugin

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
    "WorkspaceRequestError",
    "WorkspaceTextEdit",
    "WorkspaceTextEditError",
    "WorkspacePolicy",
    "WorkspaceMutation",
    "WorkspaceMutationBatch",
    "WorkspaceMutationBus",
    "WorkspaceMutationSubscription",
    "WorkspacePlugin",
    "WorkspaceService",
]
