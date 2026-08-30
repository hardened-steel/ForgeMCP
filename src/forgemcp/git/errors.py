"""Safe domain errors for the bounded read-only Git feature."""

from forgemcp.core.errors import ForgeMCPError


class GitError(ForgeMCPError):
    code = "git_error"


class GitRequestError(GitError):
    code = "git_request_error"


class GitUnavailableError(GitError):
    code = "git_unavailable"


class GitOutputError(GitError):
    code = "git_output_error"
