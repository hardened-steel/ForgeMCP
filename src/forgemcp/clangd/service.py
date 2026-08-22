"""Managed clangd lifecycle, document synchronization, and read-only LSP operations."""

from __future__ import annotations

import asyncio
import contextlib
from itertools import islice
import json
import re
import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from forgemcp.clangd.errors import (
    ClangdFailedError,
    ClangdContentModifiedError,
    ClangdEditConflictError,
    ClangdHandleExpiredError,
    ClangdNotStartedError,
    ClangdProtocolError,
    ClangdRequestError,
    ClangdRequestCancelledError,
    ClangdTimeoutError,
    ClangdUnavailableError,
    ClangdUnsupportedActionError,
    ClangdUnsupportedWorkspaceEditError,
)
from forgemcp.clangd.models import (
    CallHierarchyItem,
    CallHierarchyPrepareResult,
    ClangdSessionState,
    ClangdStartResult,
    ClangdStatus,
    CodeActionResult,
    CodeActionSummary,
    CompletionInsertTextFormat,
    CompletionItem,
    CompletionResult,
    DocumentDiagnosticsResult,
    DocumentSymbol,
    DocumentSymbolsResult,
    FormatResult,
    HoverResult,
    IncomingCall,
    IncomingCallsResult,
    NavigationResult,
    OutgoingCall,
    OutgoingCallsResult,
    RenamePreparation,
    RenameResult,
    SignatureHelpResult,
    SignatureInformation,
    SwitchSourceHeaderResult,
    TypeHierarchyItem,
    TypeHierarchyPrepareResult,
    TypeHierarchyResult,
    WorkspaceEditSummary,
    WorkspaceLocation,
    WorkspaceSymbol,
    WorkspaceSymbolsResult,
)
from forgemcp.cmake.events import CompilationDatabaseRegistry
from forgemcp.cmake.models import CompilationDatabaseStatus
from forgemcp.core.config import ForgeConfig
from forgemcp.lsp import (
    LspClient,
    LspClientState,
    LspCoordinateError,
    LspError,
    LspRequestTimeoutError,
    LspRpcError,
    PositionEncoding,
    from_lsp_range,
    to_lsp_position,
)
from forgemcp.models import Diagnostic, FileSnapshot, Position, Range, Severity
from forgemcp.processes import ProcessError, ProcessHandle, ProcessRuntime
from forgemcp.workspace import (
    WorkspaceMutationBatch,
    WorkspaceMutationBus,
    WorkspaceError,
    WorkspaceService,
    WorkspaceTextEdit,
    WorkspaceTextEditError,
)
from forgemcp.toolchain import ToolchainDiscoveryService


MAX_NAVIGATION_RESULTS = 500
MAX_DOCUMENT_SYMBOLS = 1_000
MAX_DIAGNOSTICS = 1_000
MAX_PROJECT_STATUS_DOCUMENTS = 64
MAX_TIMEOUT_SECONDS = 30.0
MAX_STDERR_CHARACTERS = 65_536
MAX_CACHE_ENTRIES = 100
HANDLE_TTL_SECONDS = 120.0
MAX_CACHED_PAYLOAD_BYTES = 65_536
MAX_WORKSPACE_EDIT_FILES = 100
MAX_WORKSPACE_EDIT_TEXT_EDITS = 1_000
MAX_WORKSPACE_EDIT_REPLACEMENT_BYTES = 1_048_576
MAX_DIRTY_DOCUMENTS = 1_024
_VERSION = re.compile(r"\bclangd version ([0-9][^\s]*)", re.IGNORECASE)
_SYMBOL_KINDS = {
    1: "file", 2: "module", 3: "namespace", 4: "package", 5: "class", 6: "method",
    7: "property", 8: "field", 9: "constructor", 10: "enum", 11: "interface", 12: "function",
    13: "variable", 14: "constant", 15: "string", 16: "number", 17: "boolean", 18: "array",
    19: "object", 20: "key", 21: "null", 22: "enum_member", 23: "struct", 24: "event",
    25: "operator", 26: "type_parameter",
}


@dataclass(slots=True)
class _DocumentState:
    """Only synchronization metadata is retained; source text is never cached."""

    path: str
    uri: str
    snapshot: FileSnapshot
    version: int
    diagnostics: tuple[Diagnostic, ...] = ()
    diagnostics_snapshot_sha256: str | None = None
    stale_diagnostics: bool = False
    diagnostic_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class ClangdProjectStatusCache:
    """Safe cached session metadata used by project status."""

    state: ClangdSessionState
    availability_observed: bool
    available: bool
    explicitly_configured: bool
    version: str | None
    compile_commands_dir: str | None
    open_document_count: int
    diagnostic_count: int
    diagnostic_error_count: int
    diagnostic_warning_count: int
    diagnostic_information_count: int
    diagnostic_hint_count: int
    stale_diagnostic_count: int
    counts_truncated: bool
    synchronization_degraded: bool


@dataclass(frozen=True, slots=True)
class _CachedAction:
    """A bounded-lifetime raw action kept only to request a safe WorkspaceEdit."""

    payload: Mapping[str, object]
    snapshots: tuple[FileSnapshot, ...]
    document_uri: str
    document_version: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class _CachedHierarchyItem:
    """Opaque server item guarded by its owning clangd session and expiry."""

    kind: str
    payload: Mapping[str, object]
    expires_at: float


class ClangdService:
    """One application-owned, workspace-scoped clangd session.

    Source text always comes from :class:`WorkspaceService`, is used only to
    synchronize or translate a single request, and is then discarded.  The
    service does not offer a generic LSP proxy: each public method maps one
    explicitly supported read-only operation into safe domain models.
    """

    def __init__(
        self, config: ForgeConfig, workspace: WorkspaceService, process_runtime: ProcessRuntime,
        toolchain: ToolchainDiscoveryService | None = None,
        mutations: WorkspaceMutationBus | None = None,
        compilation_database: CompilationDatabaseRegistry | None = None,
    ) -> None:
        self._config = config
        self._workspace = workspace
        self._process_runtime = process_runtime
        self._toolchain = toolchain
        self._mutations = mutations
        self._compilation_database_registry = compilation_database
        self._state = ClangdSessionState.STOPPED
        self._compile_commands_dir: str | None = None
        self._compile_commands_fingerprint: str | None = None
        self._position_encoding = PositionEncoding.UTF16
        self._handle: ProcessHandle | None = None
        self._client: LspClient | None = None
        self._documents: dict[str, _DocumentState] = {}
        self._dirty_generations: dict[str, int] = {}
        self._sync_pending_paths: set[str] = set()
        self._actions: dict[str, _CachedAction] = {}
        self._hierarchy_items: dict[str, _CachedHierarchyItem] = {}
        self._failure: str | None = None
        self._availability_observed = False
        self._available = False
        self._cached_version: str | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._document_lock = asyncio.Lock()
        self._mutation_lock = asyncio.Lock()
        self._watch_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_characters = 0
        self._stderr_truncated = False
        self._closing = False

    @property
    def state(self) -> ClangdSessionState:
        """Return the managed clangd state."""
        return self._state

    async def status(self) -> ClangdStatus:
        """Report a safe status and probe availability only when no session exists."""
        executable = self._executable
        if self._state is ClangdSessionState.RUNNING:
            self._availability_observed = True
            self._available = True
            return self._make_status(available=True)
        if self._state is ClangdSessionState.FAILED:
            self._availability_observed = True
            self._available = False
            return self._make_status(available=False, error=self._failure)
        try:
            result = await self._process_runtime.run([executable, "--version"], cwd=".")
        except ProcessError:
            self._availability_observed = True
            self._available = False
            return self._make_status(
                available=False,
                error="clangd was not found or is not permitted by the process policy.",
            )
        if result.timed_out or result.exit_code != 0:
            self._availability_observed = True
            self._available = False
            return self._make_status(available=False, error="clangd could not report its version successfully.")
        version_match = _VERSION.search(result.stdout.text) or _VERSION.search(result.stderr.text)
        self._availability_observed = True
        self._available = True
        self._cached_version = version_match.group(1) if version_match is not None else None
        return self._make_status(
            available=True,
            version=self._cached_version,
        )

    async def cached_project_status(self) -> ClangdProjectStatusCache:
        """Copy cached session/diagnostic counters without synchronizing a document."""

        async with self._document_lock:
            sampled_documents = tuple(islice(self._documents.values(), MAX_PROJECT_STATUS_DOCUMENTS))
            diagnostic_count = 0
            diagnostic_error_count = 0
            diagnostic_warning_count = 0
            diagnostic_information_count = 0
            diagnostic_hint_count = 0
            stale_diagnostic_count = 0
            for document in sampled_documents:
                if document.stale_diagnostics:
                    stale_diagnostic_count += len(document.diagnostics)
                for diagnostic in document.diagnostics:
                    diagnostic_count += 1
                    diagnostic_error_count += diagnostic.severity is Severity.ERROR
                    diagnostic_warning_count += diagnostic.severity is Severity.WARNING
                    diagnostic_information_count += diagnostic.severity is Severity.INFORMATION
                    diagnostic_hint_count += diagnostic.severity is Severity.HINT
            return ClangdProjectStatusCache(
                state=self._state,
                availability_observed=self._availability_observed,
                available=self._available or self._state is ClangdSessionState.RUNNING,
                explicitly_configured=self._config.clangd_path is not None,
                version=self._cached_version,
                compile_commands_dir=self._compile_commands_dir,
                open_document_count=len(self._documents),
                diagnostic_count=diagnostic_count,
                diagnostic_error_count=diagnostic_error_count,
                diagnostic_warning_count=diagnostic_warning_count,
                diagnostic_information_count=diagnostic_information_count,
                diagnostic_hint_count=diagnostic_hint_count,
                stale_diagnostic_count=stale_diagnostic_count,
                counts_truncated=len(self._documents) > MAX_PROJECT_STATUS_DOCUMENTS,
                synchronization_degraded=bool(self._sync_pending_paths) or bool(
                    self._mutations and self._mutations.degraded
                ),
            )

    async def start(self, compile_commands_dir: str | None = None) -> ClangdStartResult:
        """Start clangd with the selected validated database or fallback commands."""
        async with self._lifecycle_lock:
            directory = self._select_compile_commands_dir(compile_commands_dir)
            if self._state is ClangdSessionState.RUNNING:
                if directory != self._compile_commands_dir:
                    raise ClangdRequestError(
                        "clangd is already running with a different compile_commands_dir; stop it first."
                    )
                return ClangdStartResult(status=self._make_status(available=True))
            if self._state is ClangdSessionState.STARTING:
                raise ClangdRequestError("clangd is already starting.")
            self._state = ClangdSessionState.STARTING
            self._failure = None
            self._closing = False
            self._documents.clear()
            self._dirty_generations.clear()
            self._sync_pending_paths.clear()
            self._clear_caches()
            self._stderr_characters = 0
            self._stderr_truncated = False
            try:
                argv = [self._executable]
                if directory is not None:
                    argv.append(f"--compile-commands-dir={directory}")
                handle = await self._process_runtime.start(argv, cwd=".")
                client = LspClient(handle.stdout, handle.stdin, notification_handler=self._on_notification)
                # Retain both immediately so every failed initialize path reaps
                # the protocol child through the same managed lifecycle.
                self._handle = handle
                self._client = client
                self._stderr_task = asyncio.create_task(self._drain_stderr(handle), name="forgemcp-clangd-stderr")
                await client.start()
                response = await client.request("initialize", self._initialize_parameters(), timeout_seconds=10.0)
                self._position_encoding = self._parse_position_encoding(response)
                await client.notify("initialized", {})
                self._handle = handle
                self._client = client
                self._compile_commands_dir = directory
                selected = self._compilation_database_registry.latest if self._compilation_database_registry is not None else None
                self._compile_commands_fingerprint = (
                    selected.fingerprint if selected is not None and selected.binary_dir == directory else None
                )
                self._state = ClangdSessionState.RUNNING
                self._availability_observed = True
                self._available = True
                self._watch_task = asyncio.create_task(self._watch_process(handle), name="forgemcp-clangd-watch")
                return ClangdStartResult(status=self._make_status(available=True))
            except ProcessError as error:
                self._set_failed("clangd was not found or is not permitted by the process policy.")
                raise ClangdUnavailableError(self._failure) from error
            except (LspError, ValueError) as error:
                await self._close_partial_session()
                self._set_failed("clangd did not complete the required LSP initialization.")
                raise ClangdProtocolError(self._failure) from error
            except Exception:
                await self._close_partial_session()
                self._set_failed("clangd could not be started safely.")
                raise ClangdFailedError(self._failure)

    async def aclose(self) -> None:
        """Close open documents, perform LSP shutdown/exit, and reap clangd idempotently."""
        async with self._lifecycle_lock:
            self._closing = True
            # Do not interleave protocol shutdown with a staged filesystem
            # mutation.  Requests already waiting for clangd can still be
            # cancelled by the subsequent client close, but they cannot enter
            # the commit path once ``_closing`` is set.
            async with self._mutation_lock:
                client = self._client
                handle = self._handle
                if client is None and handle is None:
                    if self._state is not ClangdSessionState.FAILED:
                        self._state = ClangdSessionState.STOPPED
                    return
                async with self._document_lock:
                    if client is not None and client.state is LspClientState.RUNNING:
                        for document in tuple(self._documents.values()):
                            with contextlib.suppress(LspError):
                                await client.notify("textDocument/didClose", {"textDocument": {"uri": document.uri}})
                    self._documents.clear()
                    self._dirty_generations.clear()
                    self._sync_pending_paths.clear()
                    self._clear_caches()
                if client is not None and client.state is LspClientState.RUNNING:
                    with contextlib.suppress(LspError):
                        await client.request("shutdown", {}, timeout_seconds=3.0)
                    with contextlib.suppress(LspError):
                        await client.notify("exit", {})
                if client is not None:
                    await client.aclose()
                if handle is not None and handle.returncode is None:
                    try:
                        await asyncio.wait_for(handle.wait(), timeout=2.0)
                    except TimeoutError:
                        await handle.terminate()
                if self._watch_task is not None and self._watch_task is not asyncio.current_task():
                    self._watch_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await self._watch_task
                if self._stderr_task is not None:
                    self._stderr_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await self._stderr_task
                self._handle = None
                self._client = None
                self._watch_task = None
                self._stderr_task = None
                self._compile_commands_dir = None
                self._compile_commands_fingerprint = None
                if self._state is not ClangdSessionState.FAILED:
                    self._state = ClangdSessionState.STOPPED

    async def diagnostics(self, path: str, *, timeout_seconds: float | None = None) -> DocumentDiagnosticsResult:
        """Synchronize a document and await diagnostics for that exact snapshot where possible."""
        timeout = self._validate_timeout(timeout_seconds)
        document, _ = await self._synchronize_document(path)
        if document.diagnostics_snapshot_sha256 == document.snapshot.sha256:
            return self._diagnostics_result(document, complete=True, timed_out=False, stale=False)
        try:
            await asyncio.wait_for(document.diagnostic_event.wait(), timeout=timeout)
        except TimeoutError:
            return self._diagnostics_result(
                document, complete=False, timed_out=True, stale=document.stale_diagnostics
            )
        complete = document.diagnostics_snapshot_sha256 == document.snapshot.sha256
        return self._diagnostics_result(
            document, complete=complete, timed_out=False, stale=document.stale_diagnostics or not complete
        )

    async def hover(self, path: str, position: Position) -> HoverResult:
        """Return normalized hover content for a synchronized workspace document."""
        document, text = await self._synchronize_document(path)
        response = await self._request(
            "textDocument/hover",
            {"textDocument": {"uri": document.uri}, "position": self._to_lsp_position(text, position)},
        )
        if response is None:
            return HoverResult(path=document.path, snapshot=document.snapshot, document_version=document.version)
        if not isinstance(response, Mapping):
            raise ClangdProtocolError("clangd returned an invalid hover response.")
        contents = self._hover_contents(response.get("contents"))
        response_range = response.get("range")
        source_range = (
            self._from_lsp_range(text, response_range) if isinstance(response_range, Mapping) else None
        )
        return HoverResult(
            path=document.path,
            snapshot=document.snapshot,
            document_version=document.version,
            contents=contents,
            range=source_range,
        )

    async def definition(self, path: str, position: Position) -> NavigationResult:
        """Return bounded workspace-contained declaration/definition locations."""
        return await self._navigation("textDocument/definition", path, position, include_declaration=None)

    async def references(
        self, path: str, position: Position, *, include_declaration: bool = False
    ) -> NavigationResult:
        """Return bounded workspace-contained references."""
        return await self._navigation(
            "textDocument/references", path, position, include_declaration=include_declaration
        )

    async def document_symbols(self, path: str) -> DocumentSymbolsResult:
        """Return a bounded normalized hierarchy of symbols for one document."""
        document, text = await self._synchronize_document(path)
        response = await self._request("textDocument/documentSymbol", {"textDocument": {"uri": document.uri}})
        if response is None:
            values: Sequence[object] = ()
        elif isinstance(response, list):
            values = response
        else:
            raise ClangdProtocolError("clangd returned an invalid document-symbol response.")
        remaining = [MAX_DOCUMENT_SYMBOLS]
        symbols: list[DocumentSymbol] = []
        for value in values:
            symbol = self._document_symbol(value, text, remaining)
            if symbol is not None:
                symbols.append(symbol)
        return DocumentSymbolsResult(
            path=document.path,
            snapshot=document.snapshot,
            document_version=document.version,
            symbols=tuple(symbols),
            truncated=remaining[0] == 0,
        )

    async def workspace_symbols(self, query: str, *, limit: int | None = None) -> WorkspaceSymbolsResult:
        """Return a bounded workspace-only projection of workspace symbol results."""
        if not isinstance(query, str) or "\x00" in query or len(query) > 1_024:
            raise ClangdRequestError("workspace symbol queries must be NUL-free text up to 1024 characters.")
        result_limit = self._validate_limit(limit)
        response = await self._request("workspace/symbol", {"query": query})
        if response is None:
            values: Sequence[object] = ()
        elif isinstance(response, list):
            values = response
        else:
            raise ClangdProtocolError("clangd returned an invalid workspace-symbol response.")
        symbols: list[WorkspaceSymbol] = []
        omitted = 0
        truncated = False
        for value in values:
            if not isinstance(value, Mapping):
                continue
            location = self._workspace_location(value.get("location"))
            if location is None:
                omitted += 1
                continue
            if len(symbols) >= result_limit:
                truncated = True
                continue
            name = value.get("name")
            if not isinstance(name, str) or not name:
                continue
            container_name = value.get("containerName")
            symbols.append(
                WorkspaceSymbol(
                    name=name,
                    kind=self._symbol_kind(value.get("kind")),
                    container_name=container_name if isinstance(container_name, str) and container_name else None,
                    location=location,
                )
            )
        return WorkspaceSymbolsResult(
            query=query, symbols=tuple(symbols), omitted_external_results=omitted, truncated=truncated
        )

    async def completion(
        self, path: str, position: Position, *, limit: int | None = None
    ) -> CompletionResult:
        """Return bounded completion proposals; no proposal is ever applied automatically."""
        document, text = await self._synchronize_document(path)
        result_limit = self._validate_limit(limit)
        response = await self._request(
            "textDocument/completion",
            {
                "textDocument": {"uri": document.uri},
                "position": self._to_lsp_position(text, position),
                "context": {"triggerKind": 1},
            },
        )
        incomplete = False
        if response is None:
            raw_items: Sequence[object] = ()
        elif isinstance(response, list):
            raw_items = response
        elif isinstance(response, Mapping) and isinstance(response.get("items"), list):
            raw_items = response["items"]
            incomplete = response.get("isIncomplete") is True
        else:
            raise ClangdProtocolError("clangd returned an invalid completion response.")
        items: list[CompletionItem] = []
        for value in raw_items:
            if len(items) >= result_limit:
                break
            item = self._completion_item(value, text)
            if item is not None:
                items.append(item)
        return CompletionResult(
            path=document.path,
            snapshot=document.snapshot,
            document_version=document.version,
            items=tuple(items),
            is_incomplete=incomplete,
            truncated=len(raw_items) > len(items),
        )

    async def signature_help(self, path: str, position: Position) -> SignatureHelpResult:
        """Return normalized signature help for one synchronized document position."""
        document, text = await self._synchronize_document(path)
        response = await self._request(
            "textDocument/signatureHelp",
            {"textDocument": {"uri": document.uri}, "position": self._to_lsp_position(text, position)},
        )
        if response is None:
            values: Sequence[object] = ()
            active_signature = active_parameter = None
        elif isinstance(response, Mapping) and isinstance(response.get("signatures"), list):
            values = response["signatures"]
            active_signature = self._valid_index(response.get("activeSignature"))
            active_parameter = self._valid_index(response.get("activeParameter"))
        else:
            raise ClangdProtocolError("clangd returned an invalid signature-help response.")
        signatures = tuple(
            signature
            for value in values[:100]
            if (signature := self._signature_information(value)) is not None
        )
        return SignatureHelpResult(
            path=document.path,
            snapshot=document.snapshot,
            document_version=document.version,
            signatures=signatures,
            active_signature=active_signature if active_signature is not None and active_signature < len(signatures) else None,
            active_parameter=active_parameter,
            truncated=len(values) > len(signatures),
        )

    async def declaration(self, path: str, position: Position) -> NavigationResult:
        """Return bounded workspace-contained declaration locations."""
        return await self._navigation("textDocument/declaration", path, position, include_declaration=None)

    async def type_definition(self, path: str, position: Position) -> NavigationResult:
        """Return bounded workspace-contained type definition locations."""
        return await self._navigation("textDocument/typeDefinition", path, position, include_declaration=None)

    async def implementation(self, path: str, position: Position) -> NavigationResult:
        """Return bounded workspace-contained implementation locations."""
        return await self._navigation("textDocument/implementation", path, position, include_declaration=None)

    async def prepare_rename(self, path: str, position: Position) -> RenamePreparation:
        """Ask clangd whether a source range can be renamed without mutating it."""
        document, text = await self._synchronize_document(path)
        response = await self._request(
            "textDocument/prepareRename",
            {"textDocument": {"uri": document.uri}, "position": self._to_lsp_position(text, position)},
        )
        if response is None:
            return RenamePreparation(path=document.path, snapshot=document.snapshot, document_version=document.version)
        raw_range = response.get("range") if isinstance(response, Mapping) else response
        if not isinstance(raw_range, Mapping):
            raise ClangdProtocolError("clangd returned an invalid prepare-rename response.")
        placeholder = response.get("placeholder") if isinstance(response, Mapping) else None
        return RenamePreparation(
            path=document.path,
            snapshot=document.snapshot,
            document_version=document.version,
            range=self._from_lsp_range(text, raw_range),
            placeholder=placeholder if isinstance(placeholder, str) and placeholder else None,
        )

    async def rename(
        self, path: str, position: Position, new_name: str, *, expected_sha256: str | None = None
    ) -> RenameResult:
        """Apply clangd's rename WorkspaceEdit atomically through WorkspaceService."""
        if not isinstance(new_name, str) or not new_name.strip() or "\x00" in new_name or len(new_name) > 1_024:
            raise ClangdRequestError("new_name must be non-empty NUL-free text up to 1024 characters.")
        document, text = await self._synchronize_document(path)
        request_snapshot = document.snapshot
        self._require_expected_sha256(request_snapshot, expected_sha256)
        response = await self._request(
            "textDocument/rename",
            {
                "textDocument": {"uri": document.uri},
                "position": self._to_lsp_position(text, position),
                "newName": new_name,
            },
        )
        return RenameResult(
            edit=await self._apply_workspace_edit_for_snapshot(response, document, request_snapshot)
        )

    async def code_actions(
        self,
        path: str,
        source_range: Range,
        diagnostics: Sequence[Diagnostic] = (),
        kinds: Sequence[str] = (),
        *,
        limit: int | None = None,
    ) -> CodeActionResult:
        """List opaque bounded-lifetime code-action handles, never executing commands."""
        document, text = await self._synchronize_document(path)
        result_limit = self._validate_action_limit(limit)
        if isinstance(kinds, str) or not isinstance(kinds, Sequence) or any(
            not isinstance(kind, str) or not kind or len(kind) > 256 for kind in kinds
        ):
            raise ClangdRequestError("kinds must be a bounded sequence of non-empty code-action kind strings.")
        if isinstance(diagnostics, (str, bytes)) or len(diagnostics) > MAX_DIAGNOSTICS:
            raise ClangdRequestError("diagnostics must be a bounded sequence of normalized diagnostics.")
        if any(not isinstance(diagnostic, Diagnostic) for diagnostic in diagnostics):
            raise ClangdRequestError("diagnostics must be normalized Diagnostic models.")
        lsp_diagnostics = [
            {
                "range": self._to_lsp_range(text, diagnostic.location.range),
                "message": diagnostic.message,
                "severity": {Severity.ERROR: 1, Severity.WARNING: 2, Severity.INFORMATION: 3, Severity.HINT: 4}[diagnostic.severity],
                **({"code": diagnostic.code} if diagnostic.code is not None else {}),
                **({"source": diagnostic.source} if diagnostic.source is not None else {}),
            }
            for diagnostic in diagnostics
            if diagnostic.location.uri == document.uri
        ]
        response = await self._request(
            "textDocument/codeAction",
            {
                "textDocument": {"uri": document.uri},
                "range": self._to_lsp_range(text, source_range),
                "context": {
                    "diagnostics": lsp_diagnostics,
                    **({"only": list(kinds)} if kinds else {}),
                },
            },
        )
        if response is None:
            values: Sequence[object] = ()
        elif isinstance(response, list):
            values = response
        else:
            raise ClangdProtocolError("clangd returned an invalid code-action response.")
        self._purge_caches()
        summaries: list[CodeActionSummary] = []
        for value in values:
            if len(summaries) >= result_limit:
                break
            summary = self._cache_action(value, document)
            if summary is not None:
                summaries.append(summary)
        return CodeActionResult(
            path=document.path,
            snapshot=document.snapshot,
            document_version=document.version,
            actions=tuple(summaries),
            truncated=len(values) > len(summaries),
        )

    async def apply_code_action(
        self, action_id: str, *, expected_sha256: str | None = None
    ) -> WorkspaceEditSummary:
        """Resolve and apply only a pure WorkspaceEdit stored under an opaque action handle."""
        entry = self._get_action(action_id)
        payload = entry.payload
        if "command" in payload:
            raise ClangdUnsupportedActionError("Code actions with commands are not supported in this MVP.")
        if "edit" not in payload:
            response = await self._request("codeAction/resolve", payload)
            if not isinstance(response, Mapping):
                raise ClangdProtocolError("clangd returned an invalid code-action resolve response.")
            payload = response
        raw_edit = payload.get("edit")
        async with self._mutation_lock:
            document = self._current_action_document(action_id, entry, expected_sha256)
            self._require_mutation_session()
            if raw_edit is None:
                if "command" in payload:
                    raise ClangdUnsupportedActionError("Command-only code actions are not supported in this MVP.")
                return WorkspaceEditSummary(applied=True, no_op=True, affected_files=0)
            if not isinstance(raw_edit, Mapping):
                raise ClangdProtocolError("clangd returned an invalid code-action WorkspaceEdit.")
            result = await self._apply_workspace_edit_locked(raw_edit, anchor=document)
            self._actions.pop(action_id, None)
            return result

    async def format_document(self, path: str, *, expected_sha256: str | None = None) -> FormatResult:
        """Apply document-formatting edits atomically through the common edit engine."""
        document, _ = await self._synchronize_document(path)
        request_snapshot = document.snapshot
        self._require_expected_sha256(request_snapshot, expected_sha256)
        response = await self._request(
            "textDocument/formatting",
            {"textDocument": {"uri": document.uri}, "options": {"tabSize": 4, "insertSpaces": True}},
        )
        return FormatResult(
            edit=await self._apply_text_edits_response(response, document, request_snapshot)
        )

    async def format_range(
        self, path: str, source_range: Range, *, expected_sha256: str | None = None
    ) -> FormatResult:
        """Apply range-formatting edits atomically through the common edit engine."""
        document, text = await self._synchronize_document(path)
        request_snapshot = document.snapshot
        self._require_expected_sha256(request_snapshot, expected_sha256)
        response = await self._request(
            "textDocument/rangeFormatting",
            {
                "textDocument": {"uri": document.uri},
                "range": self._to_lsp_range(text, source_range),
                "options": {"tabSize": 4, "insertSpaces": True},
            },
        )
        return FormatResult(
            edit=await self._apply_text_edits_response(response, document, request_snapshot)
        )

    async def prepare_call_hierarchy(
        self, path: str, position: Position
    ) -> CallHierarchyPrepareResult:
        """Prepare opaque workspace-only call-hierarchy handles."""
        document, text = await self._synchronize_document(path)
        response = await self._request(
            "textDocument/prepareCallHierarchy",
            {"textDocument": {"uri": document.uri}, "position": self._to_lsp_position(text, position)},
        )
        return self._prepare_hierarchy(response, hierarchy_kind="call", item_type="call")

    async def incoming_calls(self, item_id: str, *, limit: int | None = None) -> IncomingCallsResult:
        """Return bounded incoming call edges for an opaque prepared item."""
        cached = self._get_hierarchy_item(item_id, "call")
        result_limit = self._validate_limit(limit)
        response = await self._request("callHierarchy/incomingCalls", {"item": cached.payload})
        values = self._response_list(response, "incoming-call")
        calls: list[IncomingCall] = []
        omitted = 0
        for value in values:
            if not isinstance(value, Mapping):
                continue
            if len(calls) >= result_limit:
                if self._hierarchy_item_data(value.get("from")) is None:
                    omitted += 1
                continue
            item = self._call_item(value.get("from"), cache=True)
            ranges = self._ranges_for_item(value.get("fromRanges"), item)
            if item is None:
                omitted += 1
            else:
                calls.append(IncomingCall(from_item=item, from_ranges=ranges))
        return IncomingCallsResult(
            item_id=item_id,
            calls=tuple(calls),
            omitted_external_results=omitted,
            truncated=len(values) > len(calls) + omitted,
        )

    async def outgoing_calls(self, item_id: str, *, limit: int | None = None) -> OutgoingCallsResult:
        """Return bounded outgoing call edges for an opaque prepared item."""
        cached = self._get_hierarchy_item(item_id, "call")
        result_limit = self._validate_limit(limit)
        response = await self._request("callHierarchy/outgoingCalls", {"item": cached.payload})
        values = self._response_list(response, "outgoing-call")
        calls: list[OutgoingCall] = []
        omitted = 0
        for value in values:
            if not isinstance(value, Mapping):
                continue
            if len(calls) >= result_limit:
                if self._hierarchy_item_data(value.get("to")) is None:
                    omitted += 1
                continue
            item = self._call_item(value.get("to"), cache=True)
            ranges = self._ranges_for_item(value.get("fromRanges"), item)
            if item is None:
                omitted += 1
            else:
                calls.append(OutgoingCall(to_item=item, from_ranges=ranges))
        return OutgoingCallsResult(
            item_id=item_id,
            calls=tuple(calls),
            omitted_external_results=omitted,
            truncated=len(values) > len(calls) + omitted,
        )

    async def prepare_type_hierarchy(
        self, path: str, position: Position
    ) -> TypeHierarchyPrepareResult:
        """Prepare opaque workspace-only type-hierarchy handles."""
        document, text = await self._synchronize_document(path)
        response = await self._request(
            "textDocument/prepareTypeHierarchy",
            {"textDocument": {"uri": document.uri}, "position": self._to_lsp_position(text, position)},
        )
        values = self._response_list(response, "type-hierarchy")
        items: list[TypeHierarchyItem] = []
        omitted = 0
        for value in values:
            if len(items) >= 100:
                if self._hierarchy_item_data(value) is None:
                    omitted += 1
                continue
            item = self._type_item(value, cache=True)
            if item is None:
                omitted += 1
            else:
                items.append(item)
        return TypeHierarchyPrepareResult(
            items=tuple(items),
            omitted_external_results=omitted,
            truncated=len(values) > len(items) + omitted,
        )

    async def supertypes(self, item_id: str, *, limit: int | None = None) -> TypeHierarchyResult:
        """Return bounded workspace-only supertypes for one prepared item."""
        return await self._type_hierarchy_relation("typeHierarchy/supertypes", item_id, limit)

    async def subtypes(self, item_id: str, *, limit: int | None = None) -> TypeHierarchyResult:
        """Return bounded workspace-only subtypes for one prepared item."""
        return await self._type_hierarchy_relation("typeHierarchy/subtypes", item_id, limit)

    async def switch_source_header(self, path: str) -> SwitchSourceHeaderResult:
        """Return a workspace-only counterpart path from clangd's extension request."""
        document, _ = await self._synchronize_document(path)
        response = await self._request("textDocument/switchSourceHeader", {"uri": document.uri})
        if response is None:
            return SwitchSourceHeaderResult(omitted_external_results=0)
        if not isinstance(response, str):
            raise ClangdProtocolError("clangd returned an invalid source/header switch response.")
        path_result = self._path_from_uri(response)
        return SwitchSourceHeaderResult(path=path_result, omitted_external_results=0 if path_result else 1)

    async def _apply_text_edits_response(
        self, response: object, document: _DocumentState, request_snapshot: FileSnapshot
    ) -> WorkspaceEditSummary:
        if response is None:
            return WorkspaceEditSummary(applied=True, no_op=True, affected_files=0)
        if not isinstance(response, list):
            raise ClangdProtocolError("clangd returned an invalid formatting edit list.")
        if not response:
            return WorkspaceEditSummary(applied=True, no_op=True, affected_files=0)
        return await self._apply_workspace_edit_for_snapshot(
            {"changes": {document.uri: response}}, document, request_snapshot
        )

    async def _apply_workspace_edit_for_snapshot(
        self, raw_edit: object, document: _DocumentState, request_snapshot: FileSnapshot
    ) -> WorkspaceEditSummary:
        """Commit only if the LSP request's anchor snapshot is still current."""
        async with self._mutation_lock:
            self._require_mutation_session()
            self._require_unchanged_document(document, request_snapshot)
            return await self._apply_workspace_edit_locked(raw_edit, anchor=document)

    async def _apply_workspace_edit(
        self, raw_edit: object, *, anchor: _DocumentState
    ) -> WorkspaceEditSummary:
        """Normalize and atomically apply only safe LSP TextDocumentEdit batches."""
        async with self._mutation_lock:
            self._require_mutation_session()
            return await self._apply_workspace_edit_locked(raw_edit, anchor=anchor)

    async def _apply_workspace_edit_locked(
        self, raw_edit: object, *, anchor: _DocumentState
    ) -> WorkspaceEditSummary:
        """Apply one WorkspaceEdit while the mutation and lifecycle boundaries are stable."""
        if raw_edit is None:
            return WorkspaceEditSummary(applied=True, no_op=True, affected_files=0)
        if not isinstance(raw_edit, Mapping):
            raise ClangdProtocolError("clangd returned an invalid WorkspaceEdit.")
        entries = self._workspace_edit_entries(raw_edit)
        if not entries:
            return WorkspaceEditSummary(applied=True, no_op=True, affected_files=0)
        expected: dict[str, FileSnapshot] = {}
        normalized: dict[str, list[WorkspaceTextEdit]] = {}
        versions: dict[str, int | None] = {}
        text_edit_count = 0
        replacement_bytes = 0
        for uri, raw_edits, version in entries:
            path = self._path_from_uri(uri)
            if path is None:
                raise ClangdUnsupportedWorkspaceEditError(
                    "WorkspaceEdit contains a URI outside the configured workspace."
                )
            if path in versions and versions[path] != version:
                raise ClangdProtocolError("WorkspaceEdit names incompatible document versions for one file.")
            versions[path] = version
            if not raw_edits:
                if version is not None:
                    open_document = self._document_for_path(path)
                    if open_document is None or version != open_document.version:
                        raise ClangdEditConflictError(
                            "A WorkspaceEdit targets a stale clangd document version."
                        )
                continue
            if path not in normalized and len(normalized) >= MAX_WORKSPACE_EDIT_FILES:
                raise ClangdProtocolError("WorkspaceEdit names more files than this service permits.")
            try:
                text, snapshot = self._workspace.read_text(path)
            except WorkspaceError as error:
                raise ClangdEditConflictError("A WorkspaceEdit target is no longer readable in the workspace.") from error
            open_document = self._document_for_path(path)
            if open_document is not None:
                if snapshot.sha256 != open_document.snapshot.sha256:
                    raise ClangdEditConflictError("A WorkspaceEdit target changed since clangd synchronized it.")
                if version is not None and version != open_document.version:
                    raise ClangdEditConflictError("A WorkspaceEdit targets a stale clangd document version.")
            elif version is not None:
                raise ClangdEditConflictError("A WorkspaceEdit version cannot be verified for an unopened document.")
            expected[path] = snapshot
            destination = normalized.setdefault(path, [])
            for raw_text_edit in raw_edits:
                if not isinstance(raw_text_edit, Mapping):
                    raise ClangdProtocolError("WorkspaceEdit contains an invalid text edit.")
                raw_range = raw_text_edit.get("range")
                new_text = raw_text_edit.get("newText")
                if not isinstance(raw_range, Mapping) or not isinstance(new_text, str):
                    raise ClangdProtocolError("WorkspaceEdit text edits require a range and string replacement.")
                text_edit_count += 1
                if text_edit_count > MAX_WORKSPACE_EDIT_TEXT_EDITS:
                    raise ClangdProtocolError("WorkspaceEdit contains more text edits than this service permits.")
                try:
                    replacement_bytes += len(new_text.encode("utf-8"))
                except UnicodeEncodeError as error:
                    raise ClangdProtocolError("WorkspaceEdit replacement text is not valid UTF-8.") from error
                if replacement_bytes > MAX_WORKSPACE_EDIT_REPLACEMENT_BYTES:
                    raise ClangdProtocolError("WorkspaceEdit replacement text exceeds this service's size limit.")
                destination.append(WorkspaceTextEdit(self._from_lsp_range(text, raw_range), new_text))
        if not normalized:
            return WorkspaceEditSummary(applied=True, no_op=True, affected_files=0)
        try:
            result = self._workspace.apply_text_edits(normalized, expected)
        except WorkspaceTextEditError as error:
            raise ClangdProtocolError("clangd returned overlapping or invalid WorkspaceEdit coordinates.") from error
        except WorkspaceError as error:
            raise ClangdEditConflictError("WorkspaceEdit could not be applied because the workspace changed.") from error
        if not result.applied:
            raise ClangdEditConflictError("WorkspaceEdit did not match the current workspace snapshots.")
        if result.changes:
            await self._synchronize_changed_documents(result.changes)
        return WorkspaceEditSummary(
            applied=True,
            no_op=not result.changes,
            changes=result.changes,
            affected_files=len(normalized),
        )

    def _require_mutation_session(self) -> None:
        """Reject a late mutation after shutdown has begun, before touching files."""
        if self._closing:
            raise ClangdNotStartedError("clangd is stopping and cannot apply a workspace edit.")
        self._require_client()

    def _current_action_document(
        self, action_id: str, entry: _CachedAction, expected_sha256: str | None
    ) -> _DocumentState:
        """Revalidate a handle after any potentially concurrent resolve request."""
        if self._actions.get(action_id) is not entry:
            raise ClangdHandleExpiredError("The code action handle was invalidated by a document change.")
        document = self._documents.get(entry.document_uri)
        if document is None or document.version != entry.document_version:
            raise ClangdHandleExpiredError("The code action belongs to a stale document version.")
        self._require_expected_sha256(document.snapshot, expected_sha256)
        current = self._workspace.get_snapshot(document.path)
        if not entry.snapshots or current.sha256 != entry.snapshots[0].sha256:
            raise ClangdEditConflictError("The code action document changed since actions were listed.")
        return document

    def _require_unchanged_document(
        self, document: _DocumentState, request_snapshot: FileSnapshot
    ) -> None:
        """Prevent a delayed mutation response from applying to a new document version."""
        if document.snapshot.sha256 != request_snapshot.sha256:
            raise ClangdEditConflictError("The document changed while clangd computed this WorkspaceEdit.")
        try:
            current = self._workspace.get_snapshot(document.path)
        except WorkspaceError as error:
            raise ClangdEditConflictError("The document changed while clangd computed this WorkspaceEdit.") from error
        if current.sha256 != request_snapshot.sha256:
            raise ClangdEditConflictError("The document changed while clangd computed this WorkspaceEdit.")

    @staticmethod
    def _workspace_edit_entries(
        raw_edit: Mapping[str, object],
    ) -> tuple[tuple[str, Sequence[object], int | None], ...]:
        """Accept only `changes` and TextDocumentEdit documentChanges, never resource operations."""
        entries: list[tuple[str, Sequence[object], int | None]] = []
        changes = raw_edit.get("changes")
        if changes is not None:
            if not isinstance(changes, Mapping):
                raise ClangdProtocolError("WorkspaceEdit changes must be an object by URI.")
            for uri, edits in changes.items():
                if not isinstance(uri, str) or not isinstance(edits, list):
                    raise ClangdProtocolError("WorkspaceEdit changes contains invalid URI edits.")
                entries.append((uri, edits, None))
        document_changes = raw_edit.get("documentChanges")
        if document_changes is not None:
            if not isinstance(document_changes, list):
                raise ClangdProtocolError("WorkspaceEdit documentChanges must be an array.")
            for change in document_changes:
                if not isinstance(change, Mapping):
                    raise ClangdProtocolError("WorkspaceEdit documentChanges contains an invalid entry.")
                if change.get("kind") in {"create", "rename", "delete"} or "kind" in change:
                    raise ClangdUnsupportedWorkspaceEditError(
                        "WorkspaceEdit resource operations are not supported in this MVP."
                    )
                document = change.get("textDocument")
                edits = change.get("edits")
                if not isinstance(document, Mapping) or not isinstance(edits, list):
                    raise ClangdProtocolError("WorkspaceEdit supports only TextDocumentEdit entries.")
                uri = document.get("uri")
                version = document.get("version")
                if not isinstance(uri, str) or (version is not None and (not isinstance(version, int) or isinstance(version, bool))):
                    raise ClangdProtocolError("WorkspaceEdit TextDocumentEdit has invalid document identity.")
                entries.append((uri, edits, version))
        return tuple(entries)

    async def _synchronize_changed_documents(self, changes: Sequence[object]) -> None:
        client = self._require_client()
        async with self._document_lock:
            for change in changes:
                uri = getattr(change, "uri", None)
                document = self._documents.get(uri) if isinstance(uri, str) else None
                if document is None:
                    continue
                try:
                    text, snapshot = self._workspace.read_text(document.path)
                except WorkspaceError as error:
                    self._set_failed("A changed open document could not be re-synchronized safely.")
                    raise ClangdFailedError(self._failure) from error
                if document.snapshot.sha256 == snapshot.sha256:
                    continue
                next_version = document.version + 1
                document.diagnostics = ()
                document.diagnostics_snapshot_sha256 = None
                document.stale_diagnostics = True
                document.diagnostic_event = asyncio.Event()
                if not await self._notify_did_change(client, document, next_version, text):
                    continue
                document.snapshot = snapshot
                document.version = next_version
                self._sync_pending_paths.discard(document.path)
        self._clear_caches()

    async def handle_workspace_mutation(self, batch: WorkspaceMutationBatch) -> None:
        """Synchronize tracked files after a Workspace post-commit event.

        Untracked changes receive only a bounded dirty marker.  The handler is
        invoked by a single application-local event worker after the filesystem
        commit/cleanup boundary, so no Workspace lock is retained while LSP is
        notified.
        """
        if self._state is not ClangdSessionState.RUNNING or self._client is None:
            for change in batch.changes:
                self._mark_dirty(change.path, batch.generation)
            return
        client = self._client
        async with self._mutation_lock:
            async with self._document_lock:
                self._clear_caches()
                for change in batch.changes:
                    document = self._document_for_path(change.path)
                    if document is None:
                        self._mark_dirty(change.path, batch.generation)
                        continue
                    after = change.after
                    if after is None or not after.exists or after.sha256 is None:
                        document.diagnostics = ()
                        document.diagnostics_snapshot_sha256 = None
                        document.stale_diagnostics = True
                        document.diagnostic_event = asyncio.Event()
                        self._mark_dirty(change.path, batch.generation)
                        continue
                    try:
                        text, snapshot = self._workspace.read_text(document.path)
                    except WorkspaceError:
                        document.stale_diagnostics = True
                        self._mark_dirty(change.path, batch.generation)
                        continue
                    if snapshot.sha256 != after.sha256:
                        document.stale_diagnostics = True
                        self._mark_dirty(change.path, batch.generation)
                        continue
                    if document.snapshot.sha256 == snapshot.sha256:
                        self._dirty_generations.pop(change.path, None)
                        continue
                    next_version = document.version + 1
                    document.diagnostics = ()
                    document.diagnostics_snapshot_sha256 = None
                    document.stale_diagnostics = True
                    document.diagnostic_event = asyncio.Event()
                    if not await self._notify_did_change(client, document, next_version, text):
                        self._mark_dirty(change.path, batch.generation)
                        continue
                    document.snapshot = snapshot
                    document.version = next_version
                    self._sync_pending_paths.discard(document.path)
                    self._dirty_generations.pop(change.path, None)

    async def handle_compilation_database_update(self, status: CompilationDatabaseStatus) -> None:
        """Perform one bounded controlled reinitialize when the DB revision changes.

        This is intentionally a database-metadata handoff, never an LSP
        extension carrying compile command contents.  Failure is recorded in
        clangd's cached state and is intentionally swallowed so CMake's
        already-successful configure response is unchanged.
        """
        if status.availability != "available" or status.binary_dir is None or status.fingerprint is None:
            return
        if self._state is not ClangdSessionState.RUNNING:
            return
        if status.binary_dir == self._compile_commands_dir and status.fingerprint == self._compile_commands_fingerprint:
            return
        if self._state is not ClangdSessionState.RUNNING:
            return
        async with self._document_lock:
            paths = tuple(document.path for document in list(self._documents.values())[:64])
        try:
            await self.aclose()
            await self.start(status.binary_dir)
            for path in paths:
                await self._synchronize_document(path)
            self._compile_commands_fingerprint = status.fingerprint
        except Exception:
            self._set_failed("clangd_reinitialize_failed")


    def _document_for_path(self, path: str) -> _DocumentState | None:
        """Find the one open document by Workspace-normalized path, not wire URI spelling."""
        return next((document for document in self._documents.values() if document.path == path), None)

    def _mark_dirty(self, path: str, generation: int) -> None:
        """Bound lazy dirty state for documents that are not currently tracked."""
        if path not in self._dirty_generations and len(self._dirty_generations) >= MAX_DIRTY_DOCUMENTS:
            self._dirty_generations.pop(next(iter(self._dirty_generations)))
        self._dirty_generations[path] = generation

    def _completion_item(self, value: object, text: str) -> CompletionItem | None:
        if not isinstance(value, Mapping):
            return None
        label = value.get("label")
        if not isinstance(label, str) or not label:
            return None
        raw_edit = value.get("textEdit")
        text_edit = None
        if isinstance(raw_edit, Mapping):
            raw_range = raw_edit.get("range")
            new_text = raw_edit.get("newText")
            if isinstance(raw_range, Mapping) and isinstance(new_text, str):
                from forgemcp.clangd.models import CompletionTextEdit

                text_edit = CompletionTextEdit(range=self._from_lsp_range(text, raw_range), new_text=new_text)
        documentation = self._documentation_text(value.get("documentation"))
        insert_text = value.get("insertText")
        return CompletionItem(
            label=label,
            kind=self._completion_kind(value.get("kind")),
            detail=value["detail"] if isinstance(value.get("detail"), str) and value["detail"] else None,
            documentation=documentation,
            insert_text=insert_text if isinstance(insert_text, str) else None,
            insert_text_format=(
                CompletionInsertTextFormat.SNIPPET
                if value.get("insertTextFormat") == 2
                else CompletionInsertTextFormat.PLAIN_TEXT
            ),
            text_edit=text_edit,
        )

    def _signature_information(self, value: object) -> SignatureInformation | None:
        if not isinstance(value, Mapping) or not isinstance(value.get("label"), str) or not value["label"]:
            return None
        parameters = value.get("parameters", [])
        labels: list[str] = []
        if isinstance(parameters, list):
            for parameter in parameters[:100]:
                if not isinstance(parameter, Mapping):
                    continue
                label = parameter.get("label")
                if isinstance(label, str):
                    labels.append(label)
                elif isinstance(label, list) and len(label) == 2 and all(isinstance(item, int) for item in label):
                    start, end = label
                    labels.append(value["label"][start:end])
        return SignatureInformation(
            label=value["label"],
            documentation=self._documentation_text(value.get("documentation")),
            parameters=tuple(labels),
        )

    @staticmethod
    def _documentation_text(value: object) -> str | None:
        if isinstance(value, str):
            return value[:16_384] or None
        if isinstance(value, Mapping) and isinstance(value.get("value"), str):
            return value["value"][:16_384] or None
        if isinstance(value, list):
            joined = "\n\n".join(item for entry in value if (item := ClangdService._documentation_text(entry)))
            return joined[:16_384] or None
        return None

    def _cache_action(self, value: object, document: _DocumentState) -> CodeActionSummary | None:
        if (
            not isinstance(value, Mapping)
            or not isinstance(value.get("title"), str)
            or not value["title"]
            or len(value["title"]) > 4_096
        ):
            return None
        payload = self._bounded_cached_payload(value)
        if payload is None:
            return None
        self._purge_caches()
        self._evict_oldest(self._actions)
        action_id = self._new_handle_id()
        self._actions[action_id] = _CachedAction(
            payload=payload,
            snapshots=(document.snapshot,),
            document_uri=document.uri,
            document_version=document.version,
            expires_at=time.monotonic() + HANDLE_TTL_SECONDS,
        )
        has_edit = isinstance(payload.get("edit"), Mapping)
        has_command = "command" in payload
        return CodeActionSummary(
            action_id=action_id,
            title=payload["title"],
            kind=(
                payload["kind"]
                if isinstance(payload.get("kind"), str) and payload["kind"] and len(payload["kind"]) <= 256
                else None
            ),
            has_workspace_edit=has_edit,
            requires_resolve=not has_edit and not has_command,
            command_only=has_command and not has_edit,
        )

    def _get_action(self, action_id: str) -> _CachedAction:
        if not isinstance(action_id, str) or not action_id:
            raise ClangdHandleExpiredError("The code action handle is invalid or expired.")
        self._purge_caches()
        entry = self._actions.get(action_id)
        if entry is None:
            raise ClangdHandleExpiredError("The code action handle is invalid, expired, or belongs to another session.")
        return entry

    def _prepare_hierarchy(
        self, response: object, *, hierarchy_kind: str, item_type: str
    ) -> CallHierarchyPrepareResult | TypeHierarchyPrepareResult:
        values = self._response_list(response, f"{hierarchy_kind}-hierarchy")
        omitted = 0
        if item_type == "call":
            items: list[CallHierarchyItem] = []
            for value in values:
                if len(items) >= 100:
                    if self._hierarchy_item_data(value) is None:
                        omitted += 1
                    continue
                item = self._call_item(value, cache=True)
                if item is None:
                    omitted += 1
                else:
                    items.append(item)
            return CallHierarchyPrepareResult(
                items=tuple(items), omitted_external_results=omitted, truncated=len(values) > len(items) + omitted
            )
        items = []
        for value in values:
            if len(items) >= 100:
                if self._hierarchy_item_data(value) is None:
                    omitted += 1
                continue
            item = self._type_item(value, cache=True)
            if item is None:
                omitted += 1
            else:
                items.append(item)
        return TypeHierarchyPrepareResult(
            items=tuple(items), omitted_external_results=omitted, truncated=len(values) > len(items) + omitted
        )

    async def _type_hierarchy_relation(
        self, method: str, item_id: str, limit: int | None
    ) -> TypeHierarchyResult:
        cached = self._get_hierarchy_item(item_id, "type")
        result_limit = self._validate_limit(limit)
        response = await self._request(method, {"item": cached.payload})
        values = self._response_list(response, "type-hierarchy")
        items: list[TypeHierarchyItem] = []
        omitted = 0
        for value in values:
            if len(items) >= result_limit:
                if self._hierarchy_item_data(value) is None:
                    omitted += 1
                continue
            item = self._type_item(value, cache=True)
            if item is None:
                omitted += 1
            else:
                items.append(item)
        return TypeHierarchyResult(
            item_id=item_id,
            items=tuple(items),
            omitted_external_results=omitted,
            truncated=len(values) > len(items) + omitted,
        )

    @staticmethod
    def _response_list(response: object, label: str) -> Sequence[object]:
        if response is None:
            return ()
        if isinstance(response, list):
            return response
        raise ClangdProtocolError(f"clangd returned an invalid {label} response.")

    def _call_item(self, value: object, *, cache: bool) -> CallHierarchyItem | None:
        parsed = self._hierarchy_item_data(value)
        if parsed is None:
            return None
        name, kind, detail, location, selection_range, payload = parsed
        item_id = self._cache_hierarchy_item("call", payload) if cache else ""
        if cache and item_id is None:
            return None
        return CallHierarchyItem(
            item_id=item_id, name=name, kind=kind, detail=detail, location=location, selection_range=selection_range
        )

    def _type_item(self, value: object, *, cache: bool) -> TypeHierarchyItem | None:
        parsed = self._hierarchy_item_data(value)
        if parsed is None:
            return None
        name, kind, detail, location, selection_range, payload = parsed
        item_id = self._cache_hierarchy_item("type", payload) if cache else ""
        if cache and item_id is None:
            return None
        return TypeHierarchyItem(
            item_id=item_id, name=name, kind=kind, detail=detail, location=location, selection_range=selection_range
        )

    def _hierarchy_item_data(
        self, value: object
    ) -> tuple[str, str, str | None, WorkspaceLocation, Range, Mapping[str, object]] | None:
        if (
            not isinstance(value, Mapping)
            or not isinstance(value.get("name"), str)
            or not value["name"]
            or len(value["name"]) > 1_024
        ):
            return None
        uri = value.get("uri")
        raw_range = value.get("range")
        raw_selection = value.get("selectionRange")
        if not isinstance(uri, str) or not isinstance(raw_range, Mapping) or not isinstance(raw_selection, Mapping):
            return None
        path = self._path_from_uri(uri)
        if path is None:
            return None
        try:
            text, _ = self._workspace.read_text(path)
            source_range = self._from_lsp_range(text, raw_range)
            selection_range = self._from_lsp_range(text, raw_selection)
        except (WorkspaceError, ClangdProtocolError):
            return None
        detail = value.get("detail")
        return (
            value["name"],
            self._symbol_kind(value.get("kind")),
            detail if isinstance(detail, str) and detail and len(detail) <= 4_096 else None,
            WorkspaceLocation(path=path, range=source_range),
            selection_range,
            dict(value),
        )

    def _ranges_for_item(self, value: object, item: CallHierarchyItem | TypeHierarchyItem | None) -> tuple[Range, ...]:
        if item is None or not isinstance(value, list):
            return ()
        try:
            text, _ = self._workspace.read_text(item.location.path)
        except WorkspaceError:
            return ()
        ranges: list[Range] = []
        for raw_range in value[:100]:
            if isinstance(raw_range, Mapping):
                with contextlib.suppress(ClangdProtocolError):
                    ranges.append(self._from_lsp_range(text, raw_range))
        return tuple(ranges)

    def _cache_hierarchy_item(self, kind: str, payload: Mapping[str, object]) -> str | None:
        bounded_payload = self._bounded_cached_payload(payload)
        if bounded_payload is None:
            return None
        self._purge_caches()
        self._evict_oldest(self._hierarchy_items)
        item_id = self._new_handle_id()
        self._hierarchy_items[item_id] = _CachedHierarchyItem(
            kind=kind, payload=bounded_payload, expires_at=time.monotonic() + HANDLE_TTL_SECONDS
        )
        return item_id

    def _get_hierarchy_item(self, item_id: str, kind: str) -> _CachedHierarchyItem:
        if not isinstance(item_id, str) or not item_id:
            raise ClangdHandleExpiredError("The hierarchy handle is invalid or expired.")
        self._purge_caches()
        item = self._hierarchy_items.get(item_id)
        if item is None or item.kind != kind:
            raise ClangdHandleExpiredError("The hierarchy handle is invalid, expired, or belongs to another session.")
        return item

    def _purge_caches(self) -> None:
        now = time.monotonic()
        self._actions = {key: value for key, value in self._actions.items() if value.expires_at > now}
        self._hierarchy_items = {
            key: value for key, value in self._hierarchy_items.items() if value.expires_at > now
        }

    def _clear_caches(self) -> None:
        self._actions.clear()
        self._hierarchy_items.clear()

    @staticmethod
    def _bounded_cached_payload(value: Mapping[str, object]) -> dict[str, object] | None:
        """Copy a raw server object only when its cache footprint is bounded."""
        try:
            encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            return None
        if len(encoded) > MAX_CACHED_PAYLOAD_BYTES:
            return None
        return dict(value)

    @staticmethod
    def _evict_oldest(cache: dict[str, object]) -> None:
        """Keep bounded caches deterministic: TTL purge first, then FIFO eviction."""
        if len(cache) >= MAX_CACHE_ENTRIES:
            cache.pop(next(iter(cache)))

    @staticmethod
    def _new_handle_id() -> str:
        return secrets.token_urlsafe(24)

    @staticmethod
    def _valid_index(value: object) -> int | None:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    @staticmethod
    def _completion_kind(value: object) -> str:
        kinds = {
            1: "text", 2: "method", 3: "function", 4: "constructor", 5: "field", 6: "variable",
            7: "class", 8: "interface", 9: "module", 10: "property", 11: "unit", 12: "value",
            13: "enum", 14: "keyword", 15: "snippet", 16: "color", 17: "file", 18: "reference",
            19: "folder", 20: "enum_member", 21: "constant", 22: "struct", 23: "event", 24: "operator",
            25: "type_parameter",
        }
        return kinds.get(value, "unknown") if isinstance(value, int) and not isinstance(value, bool) else "unknown"

    def _require_expected_sha256(self, snapshot: FileSnapshot, expected_sha256: str | None) -> None:
        if expected_sha256 is None:
            return
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ClangdRequestError("expected_sha256 must be a lowercase SHA-256 digest when supplied.")
        if snapshot.sha256 != expected_sha256:
            raise ClangdEditConflictError("The requested document snapshot does not match expected_sha256.")

    @property
    def _executable(self) -> str:
        if self._toolchain is not None:
            selected = self._toolchain.executable("clangd")
            if selected is not None:
                return str(selected)
        return str(self._config.clangd_path) if self._config.clangd_path is not None else "clangd"

    def _make_status(
        self, *, available: bool, version: str | None = None, error: str | None = None
    ) -> ClangdStatus:
        return ClangdStatus(
            executable=self._executable,
            available=available,
            state=self._state,
            version=version,
            compile_commands_dir=self._compile_commands_dir,
            error=error if error is not None else self._failure,
        )

    def _validate_compile_commands_dir(self, path: str) -> str:
        try:
            generated = self._workspace.open_generated_directory(path, create=False)
            snapshot = generated.get_snapshot("compile_commands.json")
        except WorkspaceError as error:
            raise ClangdRequestError(
                "compile_commands_dir must be an existing workspace-contained non-symlink directory."
            ) from error
        if not snapshot.exists:
            raise ClangdRequestError("compile_commands_dir must contain compile_commands.json.")
        return generated.relative_path

    def _select_compile_commands_dir(self, requested: str | None) -> str | None:
        """Use an explicit safe directory first, then the latest validated profile."""
        if requested is not None:
            if not isinstance(requested, str) or not requested:
                raise ClangdRequestError("compile_commands_dir must be a workspace-relative directory when supplied.")
            return self._validate_compile_commands_dir(requested)
        if self._compilation_database_registry is not None:
            selected = self._compilation_database_registry.latest
            if selected is not None and selected.availability == "available" and selected.binary_dir is not None:
                return self._validate_compile_commands_dir(selected.binary_dir)
        # The `off` policy deliberately starts clangd without a database flag;
        # clangd then uses its documented fallback compile-command inference.
        if self._config.compile_commands == "off":
            return None
        return None

    def _initialize_parameters(self) -> dict[str, object]:
        root_uri = self._workspace.workspace_root.as_uri()
        return {
            "processId": None,
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": "workspace"}],
            "capabilities": {
                "general": {"positionEncodings": ["utf-8", "utf-16", "utf-32"]},
                "workspace": {"workspaceEdit": {"documentChanges": True}},
                "textDocument": {
                    "publishDiagnostics": {"relatedInformation": False},
                    "completion": {"completionItem": {"snippetSupport": True}},
                    "signatureHelp": {},
                    "codeAction": {"codeActionLiteralSupport": {"codeActionKind": {"valueSet": [""]}}, "resolveSupport": {"properties": ["edit"]}},
                    "rename": {"prepareSupport": True},
                    "callHierarchy": {},
                    "typeHierarchy": {},
                },
            },
        }

    def _parse_position_encoding(self, response: object) -> PositionEncoding:
        if not isinstance(response, Mapping):
            raise ClangdProtocolError("clangd returned an invalid initialize response.")
        capabilities = response.get("capabilities")
        if not isinstance(capabilities, Mapping):
            raise ClangdProtocolError("clangd initialize response has no capabilities object.")
        value = capabilities.get("positionEncoding", "utf-16")
        try:
            return PositionEncoding(value) if isinstance(value, str) else PositionEncoding.UTF16
        except ValueError:
            return PositionEncoding.UTF16

    async def _watch_process(self, handle: ProcessHandle) -> None:
        try:
            await handle.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closing:
                self._set_failed("The managed clangd process ended unexpectedly.")
        else:
            if not self._closing:
                self._set_failed("The managed clangd process ended unexpectedly.")

    async def _drain_stderr(self, handle: ProcessHandle) -> None:
        """Continuously consume stderr without retaining or logging its raw text."""
        try:
            while chunk := await handle.stderr.read(4_096):
                decoded_length = len(chunk.decode("utf-8", errors="replace"))
                accepted = min(decoded_length, max(0, MAX_STDERR_CHARACTERS - self._stderr_characters))
                self._stderr_characters += accepted
                self._stderr_truncated = self._stderr_truncated or accepted < decoded_length
        except asyncio.CancelledError:
            raise
        except Exception:
            # stderr is diagnostic-only; protocol stdout and process watcher
            # still determine the session state.  Never publish raw stderr.
            return

    def _set_failed(self, error: str) -> None:
        self._state = ClangdSessionState.FAILED
        self._failure = error
        self._clear_caches()

    async def _close_partial_session(self) -> None:
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._stderr_task
        if self._client is not None:
            await self._client.aclose()
        if self._handle is not None and self._handle.returncode is None:
            await self._handle.aclose()
        self._client = None
        self._handle = None
        self._stderr_task = None

    async def _synchronize_document(self, path: str) -> tuple[_DocumentState, str]:
        client = self._require_client()
        try:
            text, snapshot = self._workspace.read_text(path)
        except WorkspaceError as error:
            raise ClangdRequestError("path must name a readable workspace-relative UTF-8 source file.") from error
        async with self._document_lock:
            document = self._documents.get(snapshot.uri)
            if document is None:
                document = _DocumentState(path=self._relative_path(path), uri=snapshot.uri, snapshot=snapshot, version=1)
                self._documents[snapshot.uri] = document
                await self._notify(
                    client,
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": document.uri,
                            "languageId": self._language_id(document.path),
                            "version": document.version,
                            "text": text,
                        }
                    },
                )
            elif document.snapshot.sha256 != snapshot.sha256:
                next_version = document.version + 1
                document.diagnostics = ()
                document.diagnostics_snapshot_sha256 = None
                document.stale_diagnostics = True
                document.diagnostic_event = asyncio.Event()
                self._clear_caches()
                if not await self._notify_did_change(client, document, next_version, text):
                    raise ClangdRequestError("clangd document synchronization is pending; retry the request.")
                document.snapshot = snapshot
                document.version = next_version
                self._sync_pending_paths.discard(document.path)
            self._dirty_generations.pop(document.path, None)
            return document, text

    async def _on_notification(self, method: str, params: Mapping[str, object]) -> None:
        if method != "textDocument/publishDiagnostics":
            return
        uri = params.get("uri")
        values = params.get("diagnostics")
        if not isinstance(uri, str) or not isinstance(values, list):
            return
        async with self._document_lock:
            document = self._documents.get(uri)
            if document is None:
                return
            announced_version = params.get("version")
            if isinstance(announced_version, int) and not isinstance(announced_version, bool) and announced_version != document.version:
                document.stale_diagnostics = True
                document.diagnostic_event.set()
                return
            try:
                text, current_snapshot = self._workspace.read_text(document.path)
            except WorkspaceError:
                document.stale_diagnostics = True
                document.diagnostic_event.set()
                return
            if current_snapshot.sha256 != document.snapshot.sha256:
                document.stale_diagnostics = True
                document.diagnostic_event.set()
                return
            parsed = tuple(
                diagnostic
                for value in values[:MAX_DIAGNOSTICS]
                if (diagnostic := self._diagnostic(value, document.uri, text)) is not None
            )
            document.diagnostics = parsed
            document.diagnostics_snapshot_sha256 = document.snapshot.sha256
            document.stale_diagnostics = False
            document.diagnostic_event.set()

    async def _navigation(
        self, method: str, path: str, position: Position, *, include_declaration: bool | None
    ) -> NavigationResult:
        document, text = await self._synchronize_document(path)
        params: dict[str, object] = {
            "textDocument": {"uri": document.uri},
            "position": self._to_lsp_position(text, position),
        }
        if include_declaration is not None:
            params["context"] = {"includeDeclaration": include_declaration}
        response = await self._request(method, params)
        values: Sequence[object]
        if response is None:
            values = ()
        elif isinstance(response, list):
            values = response
        elif isinstance(response, Mapping):
            values = (response,)
        else:
            raise ClangdProtocolError("clangd returned an invalid navigation response.")
        locations: list[WorkspaceLocation] = []
        omitted = 0
        truncated = False
        for value in values:
            location = self._workspace_location(value)
            if location is None:
                omitted += 1
                continue
            if len(locations) >= MAX_NAVIGATION_RESULTS:
                truncated = True
                continue
            locations.append(location)
        return NavigationResult(
            path=document.path,
            snapshot=document.snapshot,
            document_version=document.version,
            locations=tuple(locations),
            omitted_external_results=omitted,
            truncated=truncated,
        )

    async def _request(self, method: str, params: Mapping[str, object]) -> object:
        client = self._require_client()
        try:
            return await client.request(method, params, timeout_seconds=15.0)
        except LspRequestTimeoutError as error:
            raise ClangdTimeoutError("clangd did not answer before the request timeout.") from error
        except LspRpcError as error:
            if error.code == -32800:
                raise ClangdRequestCancelledError("clangd cancelled the request before it completed.") from error
            if error.code == -32801:
                raise ClangdContentModifiedError("clangd rejected the request because document content changed.") from error
            raise ClangdProtocolError("clangd rejected the request.") from error
        except LspError as error:
            if client.state is LspClientState.FAILED:
                self._set_failed("The managed clangd protocol stream failed.")
                raise ClangdFailedError(self._failure) from error
            raise ClangdProtocolError("clangd rejected or could not complete the request.") from error

    def _require_client(self) -> LspClient:
        if self._state is ClangdSessionState.FAILED:
            raise ClangdFailedError(self._failure or "The managed clangd session has failed.")
        if self._state is not ClangdSessionState.RUNNING or self._client is None:
            raise ClangdNotStartedError("Start clangd with clangd__start before requesting language features.")
        return self._client

    async def _notify(self, client: LspClient, method: str, params: Mapping[str, object]) -> None:
        try:
            await client.notify(method, params)
        except LspError as error:
            self._set_failed("The managed clangd protocol stream failed.")
            raise ClangdFailedError(self._failure) from error

    async def _notify_did_change(
        self, client: LspClient, document: _DocumentState, version: int, text: str
    ) -> bool:
        """Send one committed snapshot without falsely advancing local sync state.

        A failed notification cannot undo a Workspace commit.  Keep the prior
        document snapshot/version, mark the path pending, and let the next
        document request attempt a full resynchronization before it performs
        any LSP request.
        """
        try:
            await client.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": document.uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
        except LspError:
            if client.state is LspClientState.FAILED:
                self._set_failed("The managed clangd protocol stream failed.")
                return False
            if len(self._sync_pending_paths) >= MAX_DIRTY_DOCUMENTS and document.path not in self._sync_pending_paths:
                self._sync_pending_paths.pop()
            self._sync_pending_paths.add(document.path)
            document.stale_diagnostics = True
            document.diagnostic_event = asyncio.Event()
            return False
        return True

    def _diagnostics_result(
        self, document: _DocumentState, *, complete: bool, timed_out: bool, stale: bool
    ) -> DocumentDiagnosticsResult:
        return DocumentDiagnosticsResult(
            path=document.path,
            snapshot=document.snapshot,
            document_version=document.version,
            diagnostics=document.diagnostics if complete else (),
            complete=complete,
            timed_out=timed_out,
            stale=stale,
        )

    def _diagnostic(self, value: object, uri: str, text: str) -> Diagnostic | None:
        if not isinstance(value, Mapping):
            return None
        message = value.get("message")
        raw_range = value.get("range")
        if not isinstance(message, str) or not message.strip() or not isinstance(raw_range, Mapping):
            return None
        try:
            source_range = self._from_lsp_range(text, raw_range)
        except ClangdProtocolError:
            return None
        severity = {1: Severity.ERROR, 2: Severity.WARNING, 3: Severity.INFORMATION, 4: Severity.HINT}.get(
            value.get("severity"), Severity.INFORMATION
        )
        raw_code = value.get("code")
        code = str(raw_code) if isinstance(raw_code, (str, int)) and str(raw_code) else None
        source = value.get("source")
        return Diagnostic(
            message=message[:16_384],
            severity=severity,
            location={"uri": uri, "range": source_range},
            code=code[:256] if code else None,
            source=source[:256] if isinstance(source, str) and source else None,
        )

    def _workspace_location(self, value: object) -> WorkspaceLocation | None:
        if not isinstance(value, Mapping):
            return None
        uri = value.get("targetUri", value.get("uri"))
        raw_range = value.get("targetSelectionRange", value.get("targetRange", value.get("range")))
        if not isinstance(uri, str) or not isinstance(raw_range, Mapping):
            return None
        path = self._path_from_uri(uri)
        if path is None:
            return None
        try:
            text, _ = self._workspace.read_text(path)
            source_range = self._from_lsp_range(text, raw_range)
        except (WorkspaceError, ClangdProtocolError):
            return None
        return WorkspaceLocation(path=path, range=source_range)

    def _path_from_uri(self, uri: str) -> str | None:
        parsed = urlsplit(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            return None
        raw_path = unquote(parsed.path)
        if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
            raw_path = raw_path[1:]
        try:
            return self._workspace.validate_reported_path(raw_path)
        except WorkspaceError:
            return None

    def _document_symbol(
        self, value: object, text: str, remaining: list[int]
    ) -> DocumentSymbol | None:
        if remaining[0] <= 0 or not isinstance(value, Mapping):
            return None
        name = value.get("name")
        source_range = value.get("range")
        selection_range = value.get("selectionRange")
        if not isinstance(name, str) or not name or not isinstance(source_range, Mapping) or not isinstance(selection_range, Mapping):
            return None
        try:
            parsed_range = self._from_lsp_range(text, source_range)
            parsed_selection_range = self._from_lsp_range(text, selection_range)
        except ClangdProtocolError:
            return None
        remaining[0] -= 1
        children = value.get("children", [])
        child_symbols = (
            tuple(symbol for child in children if (symbol := self._document_symbol(child, text, remaining)) is not None)
            if isinstance(children, list)
            else ()
        )
        detail = value.get("detail")
        return DocumentSymbol(
            name=name,
            kind=self._symbol_kind(value.get("kind")),
            detail=detail if isinstance(detail, str) and detail else None,
            range=parsed_range,
            selection_range=parsed_selection_range,
            children=child_symbols,
        )

    def _from_lsp_range(self, text: str, value: Mapping[str, object]) -> Range:
        try:
            return from_lsp_range(text, value, self._position_encoding)
        except LspCoordinateError as error:
            raise ClangdProtocolError("clangd returned an invalid source coordinate.") from error

    def _to_lsp_position(self, text: str, position: Position) -> dict[str, int]:
        try:
            return to_lsp_position(text, position, self._position_encoding)
        except LspCoordinateError as error:
            raise ClangdRequestError("position must be within the current source document using zero-based code-point columns.") from error

    def _to_lsp_range(self, text: str, source_range: Range) -> dict[str, object]:
        return {
            "start": self._to_lsp_position(text, source_range.start),
            "end": self._to_lsp_position(text, source_range.end),
        }

    @staticmethod
    def _hover_contents(value: object) -> str | None:
        if isinstance(value, str):
            return value[:16_384] or None
        if isinstance(value, Mapping):
            text = value.get("value")
            return text[:16_384] if isinstance(text, str) and text else None
        if isinstance(value, list):
            parts = [ClangdService._hover_contents(item) for item in value]
            joined = "\n\n".join(part for part in parts if part)
            return joined[:16_384] or None
        return None

    @staticmethod
    def _language_id(path: str) -> str:
        suffix = PurePosixPath(path).suffix.lower()
        return "c" if suffix == ".c" else "cpp"

    @staticmethod
    def _symbol_kind(value: object) -> str:
        return _SYMBOL_KINDS.get(value, "unknown") if isinstance(value, int) and not isinstance(value, bool) else "unknown"

    def _relative_path(self, path: str) -> str:
        try:
            return self._workspace.validate_reported_path(path)
        except WorkspaceError as error:  # read_text already checked; keep an intentional boundary.
            raise ClangdRequestError("path must be workspace-relative.") from error

    @staticmethod
    def _validate_timeout(value: float | None) -> float:
        timeout = 10.0 if value is None else value
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise ClangdRequestError("timeout_seconds must be greater than zero and no more than 30 seconds.")
        return float(timeout)

    @staticmethod
    def _validate_limit(value: int | None) -> int:
        limit = 100 if value is None else value
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_NAVIGATION_RESULTS:
            raise ClangdRequestError("limit must be an integer from 1 through 500.")
        return limit

    @staticmethod
    def _validate_action_limit(value: int | None) -> int:
        limit = 50 if value is None else value
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ClangdRequestError("limit must be an integer from 1 through 100 for code actions.")
        return limit
