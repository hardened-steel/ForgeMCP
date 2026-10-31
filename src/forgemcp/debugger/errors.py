"""Recoverable debugging, reference, and adapter failures."""


class DebugError(Exception):
    """Base class for expected debugger failures."""


class DebugStateError(DebugError):
    """The requested operation is invalid in the current session state."""


class DebugUnsupportedError(DebugError):
    """The selected adapter or session mode does not support an operation."""


class DebugStaleReferenceError(DebugError):
    """An inspection handle belongs to another session or generation."""


class DebugReferenceExpiredError(DebugError):
    """A bounded execution history no longer retains the requested operation."""


class DebugAdapterError(DebugError):
    """The debugger returned invalid data or could not complete a request."""


class DebugTimeoutError(DebugError):
    """A bounded adapter request expired, possibly after changing execution."""

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        """Retain whether the caller must inspect state instead of retrying."""
        super().__init__(message)
        self.outcome_unknown = outcome_unknown
