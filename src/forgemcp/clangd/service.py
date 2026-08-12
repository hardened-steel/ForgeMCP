"""Managed clangd lifecycle, document synchronization, and read-only LSP operations."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from forgemcp.clangd.errors import (
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
from forgemcp.core.config import ForgeConfig
from forgemcp.lsp import (
    LspClient,
    LspClientState,
    LspCoordinateError,
    LspError,
    LspRequestTimeoutError,
    PositionEncoding,
    from_lsp_range,
    to_lsp_position,
)
from forgemcp.models import Diagnostic, FileSnapshot, Position, Range, Severity
from forgemcp.processes import ProcessError, ProcessHandle, ProcessRuntime
from forgemcp.workspace import WorkspaceError, WorkspaceService


MAX_NAVIGATION_RESULTS = 500
MAX_DOCUMENT_SYMBOLS = 1_000
MAX_DIAGNOSTICS = 1_000
MAX_TIMEOUT_SECONDS = 30.0
MAX_STDERR_CHARACTERS = 65_536
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


class ClangdService:
    """One application-owned, workspace-scoped clangd session.

    Source text always comes from :class:`WorkspaceService`, is used only to
    synchronize or translate a single request, and is then discarded.  The
    service does not offer a generic LSP proxy: each public method maps one
    explicitly supported read-only operation into safe domain models.
    """

    def __init__(
        self, config: ForgeConfig, workspace: WorkspaceService, process_runtime: ProcessRuntime
    ) -> None:
        self._config = config
        self._workspace = workspace
        self._process_runtime = process_runtime
        self._state = ClangdSessionState.STOPPED
        self._compile_commands_dir: str | None = None
        self._position_encoding = PositionEncoding.UTF16
        self._handle: ProcessHandle | None = None
        self._client: LspClient | None = None
        self._documents: dict[str, _DocumentState] = {}
        self._failure: str | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._document_lock = asyncio.Lock()
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
            return self._make_status(available=True)
        if self._state is ClangdSessionState.FAILED:
            return self._make_status(available=False, error=self._failure)
        try:
            result = await self._process_runtime.run([executable, "--version"], cwd=".")
        except ProcessError:
            return self._make_status(
                available=False,
                error="clangd was not found or is not permitted by the process policy.",
            )
        if result.timed_out or result.exit_code != 0:
            return self._make_status(available=False, error="clangd could not report its version successfully.")
        version_match = _VERSION.search(result.stdout.text) or _VERSION.search(result.stderr.text)
        return self._make_status(
            available=True,
            version=version_match.group(1) if version_match is not None else None,
        )

    async def start(self, compile_commands_dir: str) -> ClangdStartResult:
        """Start clangd with a validated explicit compilation database directory."""
        async with self._lifecycle_lock:
            directory = self._validate_compile_commands_dir(compile_commands_dir)
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
            self._stderr_characters = 0
            self._stderr_truncated = False
            try:
                handle = await self._process_runtime.start(
                    [self._executable, f"--compile-commands-dir={directory}"], cwd="."
                )
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
                self._state = ClangdSessionState.RUNNING
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

    @property
    def _executable(self) -> str:
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

    def _initialize_parameters(self) -> dict[str, object]:
        root_uri = self._workspace.workspace_root.as_uri()
        return {
            "processId": None,
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": "workspace"}],
            "capabilities": {
                "general": {"positionEncodings": ["utf-8", "utf-16", "utf-32"]},
                "textDocument": {"publishDiagnostics": {"relatedInformation": False}},
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
                document.snapshot = snapshot
                document.version += 1
                document.diagnostics = ()
                document.diagnostics_snapshot_sha256 = None
                document.stale_diagnostics = False
                document.diagnostic_event = asyncio.Event()
                await self._notify(
                    client,
                    "textDocument/didChange",
                    {
                        "textDocument": {"uri": document.uri, "version": document.version},
                        "contentChanges": [{"text": text}],
                    },
                )
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
        suffix = Path(path).suffix.lower()
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
