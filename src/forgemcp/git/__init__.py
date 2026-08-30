"""Read-only, workspace-bounded Git intelligence for ForgeMCP."""

from forgemcp.git.models import (
    GitBlameResult,
    GitBranchList,
    GitCommit,
    GitDiffResult,
    GitLogResult,
    GitShowCommitResult,
    GitStatus,
)
from forgemcp.git.plugin import GitPlugin
from forgemcp.git.service import GitService

__all__ = [
    "GitBlameResult", "GitBranchList", "GitCommit", "GitDiffResult",
    "GitLogResult", "GitPlugin", "GitService", "GitShowCommitResult", "GitStatus",
]
