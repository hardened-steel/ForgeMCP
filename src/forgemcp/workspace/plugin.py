"""Builtin Workspace MCP feature plugin backed solely by WorkspaceService."""

from __future__ import annotations

from collections.abc import Mapping
from collections import OrderedDict
from dataclasses import dataclass
from importlib.resources import files
import os
import re
import secrets
from time import monotonic
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from forgemcp.core.errors import ForgeMCPError, to_mcp_error_response
from forgemcp.models import FileSnapshot, Range
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import (
    AppCsp,
    AppResourceContribution,
    CompletionContribution,
    CompletionReferenceKind,
    CompletionRequest,
    ForgePlugin,
    PluginContext,
    PluginMetadata,
    ResourceContribution,
    ResourceTemplateContribution,
    ToolContribution,
    ToolAppBinding,
    ToolHints,
)
from forgemcp.workspace import (
    WorkspaceMutationBus,
    WorkspaceRequestError,
    WorkspaceService,
    WorkspaceTextEdit,
)


MAX_TOOL_PATH = 4096
MAX_TOOL_FILES = 1_000
MAX_TOOL_EDITS = 1_000
MAX_TOOL_PATCH_CHARACTERS = 1_048_576
WORKSPACE_FILES_URI = "forgemcp://workspace/files"
WORKSPACE_FILES_TEMPLATE_URI = "forgemcp://workspace/files/{cursor}"
WORKSPACE_RESULT_APP_URI = "ui://forgemcp/workspace/result"
MAX_MANIFEST_ENTRIES = 1_000
MANIFEST_PAGE_SIZE = 50
MAX_MANIFEST_CURSORS = 32
MANIFEST_CURSOR_TTL_SECONDS = 300.0
_CURSOR = re.compile(r"^[A-Za-z0-9_-]{32}$")

WorkspacePath = Annotated[str, Field(min_length=1, max_length=MAX_TOOL_PATH)]
Sha256 = Annotated[str, Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")]


class _ListFilesArguments(ForgeModel):
    path: str = Field(default=".", min_length=1, max_length=MAX_TOOL_PATH, description="Workspace-relative directory to list; defaults to the workspace root.")
    recursive: bool = Field(default=False, description="Whether to walk non-ignored descendant directories.")


class _PathArguments(ForgeModel):
    path: str = Field(min_length=1, max_length=MAX_TOOL_PATH, description="Workspace-relative UTF-8 text-file path.")


class _UnifiedPatchArguments(ForgeModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=False)
    patch: str = Field(min_length=1, max_length=MAX_TOOL_PATCH_CHARACTERS, description="Strict text-only unified patch. It may safely create a new file only when its expected SHA-256 entry is null; delete and rename operations are not exposed.")
    expected_snapshots: dict[WorkspacePath, Sha256 | None] = Field(min_length=1, max_length=MAX_TOOL_FILES, description="One path-to-current-SHA-256 expectation for every patched target. Use null only for an absent creation target; read/snapshot again before retrying a conflict.")

    @field_validator("patch")
    @classmethod
    def no_binary_patch_marker(cls, value: str) -> str:
        if "GIT binary patch" in value:
            raise ValueError("Binary patches are not supported.")
        return value


class _TextEdit(ForgeModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=False)
    range: Range = Field(description="Zero-based Unicode code-point replacement range.")
    new_text: str = Field(max_length=MAX_TOOL_PATCH_CHARACTERS, description="UTF-8 replacement text. It is never logged or returned in errors.")


TextEditCollection = Annotated[list[_TextEdit], Field(min_length=1, max_length=MAX_TOOL_EDITS)]


class _TextEditsArguments(ForgeModel):
    edits_by_path: dict[WorkspacePath, TextEditCollection] = Field(min_length=1, max_length=MAX_TOOL_FILES, description="Bounded atomic batch of existing-file edits by workspace-relative path. Creation, deletion, and rename are unavailable here.")
    expected_snapshots: dict[WorkspacePath, Sha256] = Field(min_length=1, max_length=MAX_TOOL_FILES, description="Current lowercase SHA-256 values by exactly the edited workspace-relative paths.")

    @model_validator(mode="after")
    def bounded_batch(self) -> "_TextEditsArguments":
        if sum(len(edits) for edits in self.edits_by_path.values()) > MAX_TOOL_EDITS:
            raise ValueError("The text-edit batch exceeds the configured edit collection limit.")
        total_content_bytes = 0
        for edits in self.edits_by_path.values():
            for edit in edits:
                try:
                    total_content_bytes += len(edit.new_text.encode("utf-8"))
                except UnicodeEncodeError as error:
                    raise ValueError("Text-edit replacement text must be valid UTF-8.") from error
                if total_content_bytes > MAX_TOOL_PATCH_CHARACTERS:
                    raise ValueError("The text-edit batch exceeds the configured content limit.")
        return self


class _SnapshotResult(ForgeModel):
    exists: bool
    size_bytes: int | None
    sha256: Sha256 | None
    modified_at: str | None
    captured_at: str


class _FileResult(ForgeModel):
    path: WorkspacePath
    snapshot: _SnapshotResult


class _FileChangeResult(ForgeModel):
    path: WorkspacePath
    kind: Literal["created", "modified", "deleted"]
    before: _SnapshotResult | None
    after: _SnapshotResult | None


class _ListFilesResult(ForgeModel):
    files: Annotated[list[_FileResult], Field(max_length=MAX_TOOL_FILES)]


class _ReadTextResult(ForgeModel):
    path: WorkspacePath
    text: str = Field(max_length=MAX_TOOL_PATCH_CHARACTERS)
    snapshot: _SnapshotResult


class _SnapshotToolResult(ForgeModel):
    path: WorkspacePath
    snapshot: _SnapshotResult


class _MutationResult(ForgeModel):
    applied: bool
    changes: Annotated[list[_FileChangeResult], Field(max_length=MAX_TOOL_FILES)]


class _WorkspaceErrorDetail(ForgeModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4096)


class _WorkspaceToolError(ForgeModel):
    ok: Literal[False]
    error: _WorkspaceErrorDetail


ListFilesOutput = _ListFilesResult | _WorkspaceToolError
ReadTextOutput = _ReadTextResult | _WorkspaceToolError
SnapshotOutput = _SnapshotToolResult | _WorkspaceToolError
MutationOutput = _MutationResult | _WorkspaceToolError


def _public_snapshot(snapshot: FileSnapshot) -> dict[str, object]:
    """Return snapshot metadata without its host-path-backed URI."""
    return {
        "exists": snapshot.exists,
        "size_bytes": snapshot.size_bytes,
        "sha256": snapshot.sha256,
        "modified_at": snapshot.modified_at.isoformat() if snapshot.modified_at is not None else None,
        "captured_at": snapshot.captured_at.isoformat(),
    }


def _snapshot_path(snapshot: FileSnapshot, workspace: WorkspaceService) -> str:
    """Project an internal file URI to a checked workspace-relative path."""
    parsed = urlsplit(snapshot.uri)
    if parsed.scheme != "file" or parsed.netloc:
        raise WorkspaceRequestError("Workspace returned an invalid internal file identity.")
    native = unquote(parsed.path)
    if os.name == "nt" and len(native) >= 3 and native[0] == "/" and native[2] == ":":
        native = native[1:]
    return workspace.validate_reported_path(native)


def _public_change(change: object, workspace: WorkspaceService) -> dict[str, object]:
    uri = getattr(change, "uri", None)
    snapshot = getattr(change, "after", None) or getattr(change, "before", None)
    if not isinstance(uri, str) or not isinstance(snapshot, FileSnapshot):  # pragma: no cover - service invariant
        raise RuntimeError("Workspace returned an invalid change.")
    path = _snapshot_path(snapshot, workspace)
    before = getattr(change, "before", None)
    after = getattr(change, "after", None)
    kind = getattr(change, "kind", None)
    return {
        "path": path,
        "kind": getattr(kind, "value", "modified"),
        "before": _public_snapshot(before) if isinstance(before, FileSnapshot) else None,
        "after": _public_snapshot(after) if isinstance(after, FileSnapshot) else None,
    }


@dataclass(frozen=True, slots=True)
class _ManifestEntry:
    path: str
    size_bytes: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "size_bytes": self.size_bytes, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class _ManifestCursor:
    entries: tuple[_ManifestEntry, ...]
    offset: int
    generation: int
    complete_scan: bool
    expires_at: float


class WorkspacePlugin(ForgePlugin):
    """The MCP surface for guarded Workspace reads and CAS mutations."""

    __slots__ = ("_service", "_mutations", "_manifest_cursors")

    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                plugin_id="workspace",
                requires_services=("workspace", "workspace_mutations"),
                provides=frozenset({"workspace.files"}),
            )
        )
        self._service: WorkspaceService | None = None
        self._mutations: WorkspaceMutationBus | None = None
        self._manifest_cursors: OrderedDict[str, _ManifestCursor] = OrderedDict()

    @property
    def service(self) -> WorkspaceService:
        if self._service is None:
            raise RuntimeError("The Workspace plugin is not running.")
        return self._service

    async def start(self, context: PluginContext) -> None:
        service = context.services.get("workspace")
        mutations = context.services.get("workspace_mutations")
        if not isinstance(service, WorkspaceService):
            raise TypeError("The Workspace plugin requires WorkspaceService.")
        if not isinstance(mutations, WorkspaceMutationBus):
            raise TypeError("The Workspace plugin requires WorkspaceMutationBus.")
        self._service = service
        self._mutations = mutations
        tools = (
            ("list_files", "List bounded regular non-symlink files below a workspace-relative directory. Read a file or get its snapshot before mutation; ignored/generated directories are not exposed.", _ListFilesArguments, ListFilesOutput, self._list_files, ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)),
            ("read_text", "Read one bounded UTF-8 workspace file and its SHA-256 snapshot. Before any mutation, use this or get_snapshot; after a conflict, read again before retrying.", _PathArguments, ReadTextOutput, self._read_text, ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)),
            ("get_snapshot", "Get content-free metadata and SHA-256 for one workspace-relative path. Use its SHA-256 as optimistic concurrency input before mutation.", _PathArguments, SnapshotOutput, self._get_snapshot, ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)),
            ("apply_unified_patch", "Atomically apply a strict text-only unified patch guarded by expected SHA-256 values. New files are allowed only with an expected absent (null) target; delete and rename are intentionally unavailable. On conflict, read a fresh snapshot before retrying. Successful changes synchronize active clangd documents and make CMake files stale.", _UnifiedPatchArguments, MutationOutput, self._apply_patch, ToolHints(read_only=False, destructive=True, idempotent=False, open_world=False)),
            ("apply_text_edits", "Atomically apply guarded Unicode-code-point edits to existing UTF-8 workspace files. Supply each current SHA-256; creation, deletion, and rename are unavailable. On conflict, read a fresh snapshot before retrying. Successful changes synchronize active clangd documents and make CMake files stale.", _TextEditsArguments, MutationOutput, self._apply_text_edits, ToolHints(read_only=False, destructive=True, idempotent=False, open_world=False)),
        )
        for name, description, model, output_type, operation, hints in tools:
            context.tools.register(ToolContribution(name=name, description=description, input_model=model, output_type=output_type, handler=lambda arguments, m=model, op=operation: self._dispatch(m, arguments, op), hints=hints))
        self._register_result_app(context)
        context.resources.register(
            ResourceContribution(
                uri=WORKSPACE_FILES_URI,
                name="forgemcp_workspace_files",
                description="First bounded page of deterministic workspace file metadata; no file content.",
                handler=self._manifest_first_page,
            )
        )
        context.resource_templates.register(
            ResourceTemplateContribution(
                uri_template=WORKSPACE_FILES_TEMPLATE_URI,
                name="forgemcp_workspace_files_page",
                description="Next bounded page from one application-local workspace manifest cursor.",
                arguments=("cursor",),
                handler=self._manifest_cursor_page,
            )
        )
        context.completions.register(
            CompletionContribution(
                reference_kind=CompletionReferenceKind.PROMPT,
                reference="forgemcp_analyze_file",
                argument="path",
                provider=self._complete_workspace_paths,
            )
        )
        context.completions.register(
            CompletionContribution(
                reference_kind=CompletionReferenceKind.RESOURCE_TEMPLATE,
                reference=WORKSPACE_FILES_TEMPLATE_URI,
                argument="cursor",
                provider=self._complete_manifest_cursors,
            )
        )

    @staticmethod
    def _register_result_app(context: PluginContext) -> None:
        """Register the immutable result view without changing Workspace tools."""
        try:
            html = files("forgemcp.apps.assets").joinpath("workspace-result.html").read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError, OSError, UnicodeError) as error:
            raise RuntimeError("Workspace Result App asset is unavailable.") from error
        context.apps.register_resource(
            AppResourceContribution(
                uri=WORKSPACE_RESULT_APP_URI,
                name="forgemcp_workspace_result_app",
                description="Interactive local view of one bounded Workspace tool result.",
                html=html,
                csp=AppCsp(
                    connect_domains=(), resource_domains=(), frame_domains=(), base_uri_domains=(),
                ),
                prefers_border=True,
            )
        )
        for tool_name in (
            "workspace__list_files",
            "workspace__read_text",
            "workspace__get_snapshot",
            "workspace__apply_unified_patch",
            "workspace__apply_text_edits",
        ):
            context.apps.bind_tool(
                ToolAppBinding(
                    tool_name=tool_name,
                    resource_uri=WORKSPACE_RESULT_APP_URI,
                    visibility=("model", "app"),
                )
            )

    async def stop(self) -> None:
        self._manifest_cursors.clear()
        self._mutations = None
        self._service = None

    def _manifest_first_page(self) -> dict[str, object]:
        try:
            snapshots, complete_scan = self.service.list_manifest_files(
                maximum=MAX_MANIFEST_ENTRIES
            )
            entries = tuple(
                _ManifestEntry(
                    path=_snapshot_path(snapshot, self.service),
                    size_bytes=snapshot.size_bytes or 0,
                    sha256=snapshot.sha256 or "0" * 64,
                )
                for snapshot in snapshots
                if snapshot.exists and snapshot.sha256 is not None
            )
        except ForgeMCPError:
            return self._manifest_error("manifest_unavailable")
        generation = self._mutations.generation if self._mutations is not None else 0
        return self._manifest_page(
            _ManifestCursor(
                entries=entries,
                offset=0,
                generation=generation,
                complete_scan=complete_scan,
                expires_at=monotonic() + MANIFEST_CURSOR_TTL_SECONDS,
            )
        )

    def _manifest_cursor_page(self, arguments: Mapping[str, str]) -> dict[str, object]:
        self._expire_manifest_cursors()
        token = arguments["cursor"]
        if not _CURSOR.fullmatch(token):
            return self._manifest_error("invalid_cursor")
        cursor = self._manifest_cursors.get(token)
        if cursor is None:
            return self._manifest_error("stale_cursor")
        current_generation = self._mutations.generation if self._mutations is not None else 0
        if cursor.expires_at <= monotonic() or cursor.generation != current_generation:
            self._manifest_cursors.pop(token, None)
            return self._manifest_error("stale_cursor")
        return self._manifest_page(cursor)

    def _manifest_page(self, cursor: _ManifestCursor) -> dict[str, object]:
        end = min(len(cursor.entries), cursor.offset + MANIFEST_PAGE_SIZE)
        page = cursor.entries[cursor.offset:end]
        next_cursor = None
        if end < len(cursor.entries):
            next_cursor = self._store_manifest_cursor(
                _ManifestCursor(
                    entries=cursor.entries,
                    offset=end,
                    generation=cursor.generation,
                    complete_scan=cursor.complete_scan,
                    expires_at=monotonic() + MANIFEST_CURSOR_TTL_SECONDS,
                )
            )
        truncated = not cursor.complete_scan
        return {
            "schema_version": "1",
            "resource": WORKSPACE_FILES_URI,
            "entries": [entry.as_dict() for entry in page],
            "page_size": len(page),
            "complete": next_cursor is None and cursor.complete_scan,
            "truncated": truncated,
            "next_cursor": next_cursor,
            "transactional_snapshot": False,
        }

    def _store_manifest_cursor(self, cursor: _ManifestCursor) -> str:
        self._expire_manifest_cursors()
        while len(self._manifest_cursors) >= MAX_MANIFEST_CURSORS:
            self._manifest_cursors.popitem(last=False)
        token = secrets.token_urlsafe(24)
        while token in self._manifest_cursors:  # pragma: no cover - cryptographic collision
            token = secrets.token_urlsafe(24)
        self._manifest_cursors[token] = cursor
        return token

    def _expire_manifest_cursors(self) -> None:
        now = monotonic()
        for token in tuple(self._manifest_cursors):
            if self._manifest_cursors[token].expires_at <= now:
                del self._manifest_cursors[token]

    @staticmethod
    def _manifest_error(code: str) -> dict[str, object]:
        return {
            "schema_version": "1",
            "resource": WORKSPACE_FILES_URI,
            "ok": False,
            "error": {"code": code, "message": "The requested workspace manifest page is unavailable."},
            "complete": False,
            "truncated": True,
            "next_cursor": None,
        }

    def _complete_workspace_paths(self, _request: CompletionRequest) -> tuple[str, ...]:
        try:
            paths, _ = self.service.list_file_paths(maximum=MAX_MANIFEST_ENTRIES)
        except ForgeMCPError:
            return ()
        return paths

    def _complete_manifest_cursors(self, _request: CompletionRequest) -> tuple[str, ...]:
        self._expire_manifest_cursors()
        return tuple(self._manifest_cursors)

    async def _dispatch(self, model: type[ForgeModel], arguments: Mapping[str, object], operation):
        try:
            request = model.model_validate(arguments)
            return operation(request)
        except ValidationError:
            return to_mcp_error_response(WorkspaceRequestError("Tool arguments do not match the published Workspace schema.")).as_dict()
        except ForgeMCPError as error:
            return to_mcp_error_response(error).as_dict()

    def _list_files(self, request: _ListFilesArguments) -> dict[str, object]:
        files = self.service.list_files(request.path, request.recursive)
        return {"files": [{"path": _snapshot_path(snapshot, self.service), "snapshot": _public_snapshot(snapshot)} for snapshot in files]}

    def _read_text(self, request: _PathArguments) -> dict[str, object]:
        text, snapshot = self.service.read_text(request.path)
        return {"path": _snapshot_path(snapshot, self.service), "text": text, "snapshot": _public_snapshot(snapshot)}

    def _get_snapshot(self, request: _PathArguments) -> dict[str, object]:
        snapshot = self.service.get_snapshot(request.path)
        return {"path": _snapshot_path(snapshot, self.service), "snapshot": _public_snapshot(snapshot)}

    def _apply_patch(self, request: _UnifiedPatchArguments) -> dict[str, object]:
        expected = dict(request.expected_snapshots)
        # The underlying historical Workspace capability understands deletion;
        # this new public tool deliberately does not introduce that surface.
        if any(
            line.startswith("+++ ") and line[4:].split("\t", 1)[0] == "/dev/null"
            for line in request.patch.splitlines()
        ):
            return to_mcp_error_response(WorkspaceRequestError("Delete operations are not available through this Workspace tool.")).as_dict()
        result = self.service.apply_unified_patch(request.patch, expected)
        return {"applied": result.applied, "changes": [_public_change(change, self.service) for change in result.changes]}

    def _apply_text_edits(self, request: _TextEditsArguments) -> dict[str, object]:
        edits = {
            path: tuple(WorkspaceTextEdit(edit.range, edit.new_text) for edit in file_edits)
            for path, file_edits in request.edits_by_path.items()
        }
        expected = dict(request.expected_snapshots)
        result = self.service.apply_text_edits(edits, expected)
        return {"applied": result.applied, "changes": [_public_change(change, self.service) for change in result.changes]}
