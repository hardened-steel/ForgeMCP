"""Transport-neutral immutable models for the managed clangd feature."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from forgemcp.models import Diagnostic, FileChange, FileSnapshot, Position, Range
from forgemcp.models._base import ForgeModel


class ClangdSessionState(StrEnum):
    """Lifecycle state of the application-owned clangd session."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"


class ClangdStatus(ForgeModel):
    """Safe availability and lifecycle status for the clangd feature."""

    executable: str = Field(min_length=1, description="Stable public tool identity, never its resolved executable path or argv.")
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


class CompletionInsertTextFormat(StrEnum):
    """Whether a completion proposal is plain text or an LSP snippet."""

    PLAIN_TEXT = "plain_text"
    SNIPPET = "snippet"


class _ContentBearingModel(BaseModel):
    """Strict immutable model that preserves proposed source text exactly."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=False)


class CompletionTextEdit(_ContentBearingModel):
    """A suggested completion replacement; it is never applied implicitly."""

    range: Range = Field(description="Code-point range replaced if this completion is chosen.")
    new_text: str = Field(max_length=16_384, description="Proposed insertion text or snippet.")


class CompletionItem(_ContentBearingModel):
    """Normalized, non-mutating completion suggestion."""

    label: str = Field(min_length=1, max_length=4_096, description="Completion label.")
    kind: str = Field(min_length=1, max_length=64, description="Stable textual completion-kind name.")
    detail: str | None = Field(default=None, max_length=4_096, description="Optional completion detail.")
    documentation: str | None = Field(default=None, max_length=16_384, description="Optional plain or Markdown documentation.")
    insert_text: str | None = Field(default=None, max_length=16_384, description="Suggested direct insertion text, if supplied.")
    insert_text_format: CompletionInsertTextFormat = Field(description="Whether proposed text is a snippet or plain text.")
    text_edit: CompletionTextEdit | None = Field(default=None, description="Suggested replacement range, if supplied.")


class CompletionResult(ForgeModel):
    """Bounded completion proposals for a synchronized document."""

    path: str = Field(min_length=1, description="Workspace-relative queried document path.")
    snapshot: FileSnapshot = Field(description="Snapshot used for the completion request.")
    document_version: int = Field(ge=1, description="Synchronized document version.")
    items: tuple[CompletionItem, ...] = Field(default=(), max_length=500, description="Bounded completion suggestions.")
    is_incomplete: bool = Field(description="Whether clangd reports more completion candidates may exist.")
    truncated: bool = Field(description="Whether the requested result bound was reached.")


class SignatureInformation(ForgeModel):
    """One normalized callable signature."""

    label: str = Field(min_length=1, max_length=8_192, description="Rendered callable signature.")
    documentation: str | None = Field(default=None, max_length=16_384, description="Optional signature documentation.")
    parameters: tuple[str, ...] = Field(default=(), max_length=100, description="Rendered parameter labels.")


class SignatureHelpResult(ForgeModel):
    """Normalized signature help without raw LSP fields."""

    path: str = Field(min_length=1, description="Workspace-relative queried document path.")
    snapshot: FileSnapshot = Field(description="Snapshot used for the signature-help request.")
    document_version: int = Field(ge=1, description="Synchronized document version.")
    signatures: tuple[SignatureInformation, ...] = Field(default=(), max_length=100, description="Candidate signatures.")
    active_signature: int | None = Field(default=None, ge=0, description="Selected signature index, if supplied.")
    active_parameter: int | None = Field(default=None, ge=0, description="Selected parameter index, if supplied.")
    truncated: bool = Field(description="Whether the safety bound was reached.")


class WorkspaceEditSummary(ForgeModel):
    """Content-free outcome of applying one workspace-scoped LSP edit."""

    applied: bool = Field(description="Whether the complete edit batch was committed.")
    no_op: bool = Field(description="Whether clangd supplied no effective source changes.")
    changes: tuple[FileChange, ...] = Field(default=(), description="Atomic metadata-only file changes after success.")
    affected_files: int = Field(ge=0, description="Number of workspace files named by the edit.")


class RenamePreparation(ForgeModel):
    """The range clangd permits to be renamed at one source position."""

    path: str = Field(min_length=1, description="Workspace-relative queried document path.")
    snapshot: FileSnapshot = Field(description="Snapshot used to prepare the rename.")
    document_version: int = Field(ge=1, description="Synchronized document version.")
    range: Range | None = Field(default=None, description="Renameable code-point range; null means no rename is valid.")
    placeholder: str | None = Field(default=None, max_length=1_024, description="Optional server-provided rename placeholder.")


class RenameResult(ForgeModel):
    """Content-free result of a workspace-scoped rename."""

    edit: WorkspaceEditSummary = Field(description="Atomic WorkspaceEdit outcome.")


class FormatResult(ForgeModel):
    """Content-free result of document or range formatting."""

    edit: WorkspaceEditSummary = Field(description="Atomic formatting WorkspaceEdit outcome.")


class CodeActionSummary(ForgeModel):
    """An opaque session-bound action that may be safe to apply later."""

    action_id: str = Field(min_length=1, max_length=128, description="Opaque bounded-lifetime session action handle.")
    title: str = Field(min_length=1, max_length=4_096, description="User-facing action title.")
    kind: str | None = Field(default=None, max_length=256, description="Optional LSP code-action kind.")
    has_workspace_edit: bool = Field(description="Whether a pure WorkspaceEdit is already available.")
    requires_resolve: bool = Field(description="Whether the action must be resolved before it may expose an edit.")
    command_only: bool = Field(description="Whether the action is command-only and unsupported in this MVP.")


class CodeActionResult(ForgeModel):
    """Bounded code-action summaries for a synchronized document range."""

    path: str = Field(min_length=1, description="Workspace-relative queried document path.")
    snapshot: FileSnapshot = Field(description="Snapshot used to list code actions.")
    document_version: int = Field(ge=1, description="Synchronized document version.")
    actions: tuple[CodeActionSummary, ...] = Field(default=(), max_length=100, description="Opaque action summaries.")
    truncated: bool = Field(description="Whether the requested result bound was reached.")


class CallHierarchyItem(ForgeModel):
    """A workspace-contained opaque call-hierarchy handle."""

    item_id: str = Field(min_length=1, max_length=128, description="Opaque bounded-lifetime call-hierarchy handle.")
    name: str = Field(min_length=1, max_length=1_024, description="Symbol name.")
    kind: str = Field(min_length=1, max_length=64, description="Stable textual symbol-kind name.")
    detail: str | None = Field(default=None, max_length=4_096, description="Optional safe item detail.")
    location: WorkspaceLocation = Field(description="Workspace-contained item range.")
    selection_range: Range = Field(description="Code-point selection range.")


class CallHierarchyPrepareResult(ForgeModel):
    """Bounded initial call-hierarchy items."""

    items: tuple[CallHierarchyItem, ...] = Field(default=(), max_length=100, description="Prepared workspace items.")
    omitted_external_results: int = Field(ge=0, description="External items intentionally omitted.")
    truncated: bool = Field(description="Whether the safety bound was reached.")


class IncomingCall(ForgeModel):
    """One caller edge ending at a prepared call-hierarchy item."""

    from_item: CallHierarchyItem = Field(description="Workspace-contained calling item.")
    from_ranges: tuple[Range, ...] = Field(default=(), max_length=100, description="Call-site code-point ranges.")


class OutgoingCall(ForgeModel):
    """One callee edge starting at a prepared call-hierarchy item."""

    to_item: CallHierarchyItem = Field(description="Workspace-contained called item.")
    from_ranges: tuple[Range, ...] = Field(default=(), max_length=100, description="Call-site code-point ranges.")


class IncomingCallsResult(ForgeModel):
    """Bounded incoming call edges."""

    item_id: str = Field(min_length=1, max_length=128, description="Queried opaque call-hierarchy handle.")
    calls: tuple[IncomingCall, ...] = Field(default=(), max_length=500, description="Incoming call edges.")
    omitted_external_results: int = Field(ge=0, description="External edges intentionally omitted.")
    truncated: bool = Field(description="Whether the requested result bound was reached.")


class OutgoingCallsResult(ForgeModel):
    """Bounded outgoing call edges."""

    item_id: str = Field(min_length=1, max_length=128, description="Queried opaque call-hierarchy handle.")
    calls: tuple[OutgoingCall, ...] = Field(default=(), max_length=500, description="Outgoing call edges.")
    omitted_external_results: int = Field(ge=0, description="External edges intentionally omitted.")
    truncated: bool = Field(description="Whether the requested result bound was reached.")


class TypeHierarchyItem(ForgeModel):
    """A workspace-contained opaque type-hierarchy handle."""

    item_id: str = Field(min_length=1, max_length=128, description="Opaque bounded-lifetime type-hierarchy handle.")
    name: str = Field(min_length=1, max_length=1_024, description="Type symbol name.")
    kind: str = Field(min_length=1, max_length=64, description="Stable textual symbol-kind name.")
    detail: str | None = Field(default=None, max_length=4_096, description="Optional safe type detail.")
    location: WorkspaceLocation = Field(description="Workspace-contained item range.")
    selection_range: Range = Field(description="Code-point selection range.")


class TypeHierarchyPrepareResult(ForgeModel):
    """Bounded initial type-hierarchy items."""

    items: tuple[TypeHierarchyItem, ...] = Field(default=(), max_length=100, description="Prepared workspace type items.")
    omitted_external_results: int = Field(ge=0, description="External items intentionally omitted.")
    truncated: bool = Field(description="Whether the safety bound was reached.")


class TypeHierarchyResult(ForgeModel):
    """Bounded supertype or subtype results for a cached type handle."""

    item_id: str = Field(min_length=1, max_length=128, description="Queried opaque type-hierarchy handle.")
    items: tuple[TypeHierarchyItem, ...] = Field(default=(), max_length=500, description="Related workspace type items.")
    omitted_external_results: int = Field(ge=0, description="External items intentionally omitted.")
    truncated: bool = Field(description="Whether the requested result bound was reached.")


class SwitchSourceHeaderResult(ForgeModel):
    """A workspace-only source/header counterpart returned by clangd."""

    path: str | None = Field(default=None, description="Workspace-relative counterpart path, if available.")
    omitted_external_results: int = Field(ge=0, description="One when clangd named an external counterpart that was omitted.")
