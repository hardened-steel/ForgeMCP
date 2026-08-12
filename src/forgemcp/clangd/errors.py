"""Expected safe errors owned by the clangd feature module."""

from __future__ import annotations

from forgemcp.core.errors import ForgeMCPError


class ClangdError(ForgeMCPError):
    """Base class for safe clangd feature failures."""

    code = "clangd_error"


class ClangdRequestError(ClangdError):
    """A request does not meet the public clangd tool contract."""

    code = "clangd_request_error"


class ClangdUnavailableError(ClangdError):
    """clangd is absent or denied by the configured Process Runtime."""

    code = "clangd_unavailable"


class ClangdNotStartedError(ClangdError):
    """A navigation operation requires a running managed clangd session."""

    code = "clangd_not_started"


class ClangdFailedError(ClangdError):
    """The managed clangd session crashed or its protocol failed."""

    code = "clangd_failed"


class ClangdProtocolError(ClangdError):
    """clangd returned a malformed or unsupported protocol result."""

    code = "clangd_protocol_error"


class ClangdTimeoutError(ClangdError):
    """A bounded clangd request did not finish in time."""

    code = "clangd_timeout"
