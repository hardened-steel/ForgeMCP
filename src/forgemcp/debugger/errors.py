"""Safe domain errors for the managed debugger feature."""

from __future__ import annotations

from forgemcp.core.errors import ForgeMCPError


class DebuggerError(ForgeMCPError):
    code = "debugger_error"


class DebuggerUnavailableError(DebuggerError):
    code = "debugger_unavailable"


class DebuggerStateError(DebuggerError):
    code = "debugger_invalid_state"


class DebuggerSessionActiveError(DebuggerError):
    code = "debugger_session_active"


class DebuggerHandleExpiredError(DebuggerError):
    code = "debugger_handle_expired"


class DebuggerRequestError(DebuggerError):
    code = "debugger_request_error"


class DebuggerUnsupportedError(DebuggerError):
    code = "debugger_unsupported"


class DebuggerStaleDataError(DebuggerError):
    code = "debugger_stopped_data_stale"


class DebuggerFailedError(DebuggerError):
    code = "debugger_failed"
