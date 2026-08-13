"""Safe DAP transport failures.

These errors deliberately never contain adapter payload, stdout, or stderr.
"""

from __future__ import annotations


class DapError(Exception):
    """Base class for expected DAP-client failures."""


class DapConnectionClosedError(DapError):
    """The adapter protocol stream is not usable."""


class DapProtocolError(DapError):
    """The adapter sent malformed or structurally invalid DAP data."""


class DapRequestTimeoutError(DapError):
    """The adapter did not reply before a bounded request deadline."""


class DapRequestError(DapError):
    """The adapter returned a structurally valid unsuccessful response."""

    def __init__(self, command: str, message: str | None = None) -> None:
        self.command = command
        super().__init__(message or f"The debug adapter rejected '{command}'.")
