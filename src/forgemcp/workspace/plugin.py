"""Builtin Workspace MCP feature plugin backed solely by WorkspaceService."""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Annotated
from urllib.parse import unquote, urlsplit

from pydantic import ConfigDict, Field, ValidationError, field_validator

from forgemcp.core.errors import ForgeMCPError, to_mcp_error_response
from forgemcp.models import FileSnapshot, Range
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import ForgePlugin, PluginContext, PluginMetadata, ToolContribution, ToolHints
from forgemcp.workspace import WorkspaceRequestError, WorkspaceService, WorkspaceTextEdit


MAX_TOOL_PATH = 4096
MAX_TOOL_FILES = 1_000
MAX_TOOL_EDITS = 1_000
MAX_TOOL_PATCH_CHARACTERS = 1_048_576

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


class _TextEditsArguments(ForgeModel):
    edits_by_path: dict[WorkspacePath, list[_TextEdit]] = Field(min_length=1, max_length=MAX_TOOL_FILES, description="Bounded atomic batch of existing-file edits by workspace-relative path. Creation, deletion, and rename are unavailable here.")
    expected_snapshots: dict[WorkspacePath, Sha256] = Field(min_length=1, max_length=MAX_TOOL_FILES, description="Current lowercase SHA-256 values by exactly the edited workspace-relative paths.")


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


class WorkspacePlugin(ForgePlugin):
    """The MCP surface for guarded Workspace reads and CAS mutations."""

    __slots__ = ("_service",)

    def __init__(self) -> None:
        super().__init__(PluginMetadata(plugin_id="workspace", requires_services=("workspace",), provides=frozenset({"workspace.files"})))
        self._service: WorkspaceService | None = None

    @property
    def service(self) -> WorkspaceService:
        if self._service is None:
            raise RuntimeError("The Workspace plugin is not running.")
        return self._service

    async def start(self, context: PluginContext) -> None:
        service = context.services.get("workspace")
        if not isinstance(service, WorkspaceService):
            raise TypeError("The Workspace plugin requires WorkspaceService.")
        self._service = service
        tools = (
            ("list_files", "List bounded regular non-symlink files below a workspace-relative directory. Read a file or get its snapshot before mutation; ignored/generated directories are not exposed.", _ListFilesArguments, self._list_files, ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)),
            ("read_text", "Read one bounded UTF-8 workspace file and its SHA-256 snapshot. Before any mutation, use this or get_snapshot; after a conflict, read again before retrying.", _PathArguments, self._read_text, ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)),
            ("get_snapshot", "Get content-free metadata and SHA-256 for one workspace-relative path. Use its SHA-256 as optimistic concurrency input before mutation.", _PathArguments, self._get_snapshot, ToolHints(read_only=True, destructive=False, idempotent=True, open_world=False)),
            ("apply_unified_patch", "Atomically apply a strict text-only unified patch guarded by expected SHA-256 values. New files are allowed only with an expected absent (null) target; delete and rename are intentionally unavailable. On conflict, read a fresh snapshot before retrying. Successful changes synchronize active clangd documents and make CMake files stale.", _UnifiedPatchArguments, self._apply_patch, ToolHints(read_only=False, destructive=True, idempotent=False, open_world=False)),
            ("apply_text_edits", "Atomically apply guarded Unicode-code-point edits to existing UTF-8 workspace files. Supply each current SHA-256; creation, deletion, and rename are unavailable. On conflict, read a fresh snapshot before retrying. Successful changes synchronize active clangd documents and make CMake files stale.", _TextEditsArguments, self._apply_text_edits, ToolHints(read_only=False, destructive=True, idempotent=False, open_world=False)),
        )
        for name, description, model, operation, hints in tools:
            context.tools.register(ToolContribution(name=name, description=description, input_model=model, handler=lambda arguments, m=model, op=operation: self._dispatch(m, arguments, op), hints=hints))

    async def stop(self) -> None:
        self._service = None

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
        if "--- /dev/null" not in request.patch and "+++ /dev/null" in request.patch:
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
