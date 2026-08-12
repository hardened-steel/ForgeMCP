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


class ClangdEditConflictError(ClangdError):
    """A WorkspaceEdit no longer matches the snapshots it was computed from."""

    code = "clangd_edit_conflict"


class ClangdUnsupportedWorkspaceEditError(ClangdError):
    """A server edit contains an unsafe resource operation or external target."""

    code = "clangd_workspace_edit_unsupported"


class ClangdUnsupportedActionError(ClangdError):
    """A code action would require a command or unsupported execution path."""

    code = "clangd_code_action_unsupported"


class ClangdHandleExpiredError(ClangdError):
    """An opaque action or hierarchy handle is stale, expired, or from another session."""

    code = "clangd_handle_expired"


class ClangdRequestCancelledError(ClangdError):
    """clangd cancelled the request before producing a stable result."""

    code = "clangd_request_cancelled"


class ClangdContentModifiedError(ClangdError):
    """clangd rejected a request because its document state changed."""

    code = "clangd_content_modified"
