"""Transport-neutral LSP framing, JSON-RPC client, and coordinate adapters."""

from forgemcp.lsp.client import LspClient, LspClientState
from forgemcp.lsp.errors import (
    LspConnectionClosedError,
    LspCoordinateError,
    LspError,
    LspProtocolError,
    LspRequestTimeoutError,
    LspRpcError,
)
from forgemcp.lsp.positions import PositionEncoding, from_lsp_position, from_lsp_range, to_lsp_position

__all__ = [
    "LspClient",
    "LspClientState",
    "LspConnectionClosedError",
    "LspCoordinateError",
    "LspError",
    "LspProtocolError",
    "LspRequestTimeoutError",
    "LspRpcError",
    "PositionEncoding",
    "from_lsp_position",
    "from_lsp_range",
    "to_lsp_position",
]
