"""Public transport-neutral API for ForgeMCP's managed clangd feature."""

from forgemcp.clangd.errors import (
    ClangdError,
    ClangdFailedError,
    ClangdNotStartedError,
    ClangdProtocolError,
    ClangdRequestError,
    ClangdTimeoutError,
    ClangdUnavailableError,
)
from forgemcp.clangd.models import (
    ClangdSessionState,
    ClangdStartResult,
    ClangdStatus,
    DocumentDiagnosticsResult,
    DocumentSymbol,
    DocumentSymbolsResult,
    HoverResult,
    NavigationResult,
    WorkspaceLocation,
    WorkspaceSymbol,
    WorkspaceSymbolsResult,
)
from forgemcp.clangd.plugin import ClangdPlugin
from forgemcp.clangd.service import ClangdService

__all__ = [
    "ClangdError",
    "ClangdFailedError",
    "ClangdNotStartedError",
    "ClangdPlugin",
    "ClangdProtocolError",
    "ClangdRequestError",
    "ClangdService",
    "ClangdSessionState",
    "ClangdStartResult",
    "ClangdStatus",
    "ClangdTimeoutError",
    "ClangdUnavailableError",
    "DocumentDiagnosticsResult",
    "DocumentSymbol",
    "DocumentSymbolsResult",
    "HoverResult",
    "NavigationResult",
    "WorkspaceLocation",
    "WorkspaceSymbol",
    "WorkspaceSymbolsResult",
]
