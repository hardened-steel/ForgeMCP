"""Expected language analysis failures, without protocol or transcript dumps."""


class ClangdError(Exception):
    """Base error for an unavailable or unsuccessful analysis."""


class ClangdSessionError(ClangdError):
    """A language-server session could not be established or was lost."""


class ClangdTimeoutError(ClangdError):
    """A request or version-specific diagnostic exceeded its deadline."""


class ClangdProtocolError(ClangdError):
    """The language server returned an unsupported or malformed result."""


class ClangdRequestError(ClangdError):
    """The language server rejected an operation."""

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method} failed ({code}): {message}")
        self.code = code


class ClangdStaleResultError(ClangdError):
    """Workspace content changed while an analysis snapshot was being produced."""
