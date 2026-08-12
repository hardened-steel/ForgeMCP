"""Transport-neutral immutable models for the managed clangd feature."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from forgemcp.models import Diagnostic, FileSnapshot, Position, Range
from forgemcp.models._base import ForgeModel


class ClangdSessionState(StrEnum):
    """Lifecycle state of the application-owned clangd session."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"


class ClangdStatus(ForgeModel):
    """Safe availability and lifecycle status for the clangd feature."""

    executable: str = Field(min_length=1, description="Configured clangd executable selector, never its argv.")
    available: bool = Field(description="Whether clangd is available through the Process Runtime.")
    state: ClangdSessionState = Field(description="Current managed-session lifecycle state.")
    version: str | None = Field(default=None, max_length=256, description="Parsed clangd version when available.")
    compile_commands_dir: str | None = Field(default=None, description="Active workspace-relative compilation database directory.")
    error: str | None = Field(default=None, max_length=512, description="Safe availability or lifecycle failure summary.")


class ClangdStartResult(ForgeModel):
    """The result of explicitly creating and initializing one clangd session."""

    status: ClangdStatus = Field(description="Status after the requested start attempt.")


class DocumentDiagnosticsResult(ForgeModel):
    """Diagnostics bound to a particular synchronized document snapshot and version."""

    path: str = Field(min_length=1, description="Workspace-relative document path.")
    snapshot: FileSnapshot = Field(description="Snapshot whose text was synchronized with clangd.")
    document_version: int = Field(ge=1, description="Monotonic LSP document version for this session.")
    diagnostics: tuple[Diagnostic, ...] = Field(default=(), max_length=1_000, description="Bounded current diagnostics.")
    complete: bool = Field(description="Whether clangd published diagnostics for this exact snapshot.")
    timed_out: bool = Field(description="Whether the bounded diagnostic wait expired.")
    stale: bool = Field(description="Whether clangd supplied diagnostics for another document version.")


class HoverResult(ForgeModel):
    """A normalized hover response, without a raw LSP payload."""

    path: str = Field(min_length=1, description="Workspace-relative queried document path.")
    snapshot: FileSnapshot = Field(description="Snapshot used for the hover request.")
    document_version: int = Field(ge=1, description="Synchronized document version.")
    contents: str | None = Field(default=None, max_length=16_384, description="Plain or Markdown hover text, when supplied.")
    range: Range | None = Field(default=None, description="Hovered source range, when supplied.")


class WorkspaceLocation(ForgeModel):
    """A location confirmed to remain inside the configured workspace."""

    path: str = Field(min_length=1, description="Workspace-relative source path.")
    range: Range = Field(description="Code-point source range.")


class NavigationResult(ForgeModel):
    """Bounded definition or reference locations with an explicit external-result policy."""

    path: str = Field(min_length=1, description="Workspace-relative queried document path.")
    snapshot: FileSnapshot = Field(description="Snapshot used for the request.")
    document_version: int = Field(ge=1, description="Synchronized document version.")
    locations: tuple[WorkspaceLocation, ...] = Field(default=(), max_length=500, description="Workspace-contained result locations.")
    omitted_external_results: int = Field(ge=0, description="Results outside the workspace intentionally omitted from the API.")
    truncated: bool = Field(description="Whether the returned workspace result list hit its requested bound.")


class DocumentSymbol(ForgeModel):
    """A normalized hierarchical document symbol."""

    name: str = Field(min_length=1, max_length=1_024, description="Symbol display name.")
    kind: str = Field(min_length=1, max_length=64, description="Stable textual LSP symbol-kind name.")
    detail: str | None = Field(default=None, max_length=4_096, description="Optional safe symbol detail.")
    range: Range = Field(description="Full code-point range occupied by the symbol.")
    selection_range: Range = Field(description="Code-point range used to select the symbol.")
    children: tuple["DocumentSymbol", ...] = Field(default=(), description="Nested symbols, within the result bound.")


class DocumentSymbolsResult(ForgeModel):
    """Bounded document symbols for a synchronized workspace file."""

    path: str = Field(min_length=1, description="Workspace-relative queried document path.")
    snapshot: FileSnapshot = Field(description="Snapshot used for the request.")
    document_version: int = Field(ge=1, description="Synchronized document version.")
    symbols: tuple[DocumentSymbol, ...] = Field(default=(), description="Document symbols.")
    truncated: bool = Field(description="Whether the symbol tree hit its safety bound.")


class WorkspaceSymbol(ForgeModel):
    """A workspace symbol whose location remains in the workspace."""

    name: str = Field(min_length=1, max_length=1_024, description="Symbol display name.")
    kind: str = Field(min_length=1, max_length=64, description="Stable textual LSP symbol-kind name.")
    container_name: str | None = Field(default=None, max_length=1_024, description="Optional containing symbol name.")
    location: WorkspaceLocation = Field(description="Workspace-contained symbol location.")


class WorkspaceSymbolsResult(ForgeModel):
    """Bounded workspace-symbol results with external locations omitted."""

    query: str = Field(max_length=1_024, description="Requested workspace-symbol query.")
    symbols: tuple[WorkspaceSymbol, ...] = Field(default=(), max_length=500, description="Workspace-contained matching symbols.")
    omitted_external_results: int = Field(ge=0, description="External result locations intentionally omitted from the API.")
    truncated: bool = Field(description="Whether the workspace result list hit its requested bound.")


DocumentSymbol.model_rebuild()
