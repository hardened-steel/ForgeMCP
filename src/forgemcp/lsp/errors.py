"""Errors raised by the transport-neutral JSON-RPC/LSP client."""

from __future__ import annotations


class LspError(Exception):
    """Base error for an LSP transport failure without wire-payload exposure."""


class LspConnectionClosedError(LspError):
    """The language-server stream ended before a request completed."""


class LspProtocolError(LspError):
    """The peer sent malformed JSON-RPC or invalid LSP framing."""


class LspRequestTimeoutError(LspError):
    """A request did not receive a response before its declared timeout."""


class LspRpcError(LspError):
    """The language server returned a JSON-RPC error response."""


class LspCoordinateError(LspError):
    """A coordinate cannot be represented in the negotiated LSP encoding."""
