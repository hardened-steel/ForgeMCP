"""One language-server session: requests, capabilities, and document versions."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from pydantic import JsonValue, ValidationError

from forgemcp.cmake.service import CompilationContext
from forgemcp.progress import Progress
from forgemcp.toolchain.errors import ToolchainError
from forgemcp.toolchain.tools.clangd import (
    Connection,
    LspErrorResponse,
    LspNotification,
    LspRequest,
    LspResponse,
)
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import WorkspaceService

from .errors import (
    ClangdError,
    ClangdProtocolError,
    ClangdRequestError,
    ClangdSessionError,
    ClangdStaleResultError,
    ClangdTimeoutError,
)
from .models import Diagnostic, Location, Position, RelatedDiagnostic, SourceRange


@dataclass
class Document:
    path: WorkspacePath
    text: str
    version: int = 0
    diagnostics_version: int | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)


def object_value(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ClangdProtocolError("Expected an LSP object.")
    return value


def list_value(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ClangdProtocolError("Expected an LSP array.")
    return value


def integer(value: JsonValue) -> int:
    if type(value) is not int or value < 0:
        raise ClangdProtocolError("Expected a nonnegative LSP integer.")
    return value


def string(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise ClangdProtocolError("Expected LSP text.")
    return value


def position(value: JsonValue) -> Position:
    data = object_value(value)
    return Position(
        line=integer(data.get("line")) + 1,
        character=integer(data.get("character")),
    )


def source_range(value: JsonValue) -> SourceRange:
    data = object_value(value)
    try:
        return SourceRange(
            start=position(data.get("start")),
            end=position(data.get("end")),
        )
    except ValidationError as error:
        raise ClangdProtocolError("Invalid source range from clangd.") from error


def lsp_position(value: Position) -> dict[str, JsonValue]:
    return {"line": value.line - 1, "character": value.character}


def validate_timeout(timeout: float) -> None:
    if not isfinite(timeout) or timeout <= 0:
        raise ClangdError("Analysis timeout must be finite and greater than zero.")


class ClangdSession:
    """Interpret LSP for one compilation context; never own a subprocess directly."""

    def __init__(
        self,
        connection: Connection,
        workspace: WorkspaceService,
        configuration: CompilationContext,
    ) -> None:
        self.connection = connection
        self.workspace = workspace
        self.configuration = configuration
        self.capabilities: dict[str, JsonValue] = {}
        self.pending: dict[int, asyncio.Future[JsonValue]] = {}
        self.methods: dict[int, str] = {}
        self.documents: dict[str, Document] = {}
        self.versions: dict[str, int] = {}
        self.next_id = 0
        self.reader: asyncio.Task[None] | None = None
        self.condition = asyncio.Condition()
        self.failure: ClangdError | None = None
        self.initialized = False
        self.closing = False

    async def report(self, callback: Progress | None, message: str) -> None:
        if callback is not None:
            await callback(f"{self.configuration.id}: {message}")

    def check_alive(self) -> None:
        if self.failure is not None:
            raise self.failure
        if self.closing:
            raise ClangdSessionError("clangd session is closing.")

    def uri(self, path: WorkspacePath) -> str:
        for uri, document in self.documents.items():
            if document.path == path:
                return uri
        native = path.absolute if path.area == "root" else self.workspace.resolve_workspace_path(path)
        return native.resolve().as_uri()

    def path(self, uri: str) -> WorkspacePath:
        parts = urlsplit(uri)
        if parts.scheme != "file" or parts.query or parts.fragment:
            raise ClangdProtocolError("clangd returned a non-file location.")
        if parts.netloc and parts.netloc.lower() != "localhost":
            if os.name != "nt":
                raise ClangdProtocolError("A remote file URI is not a local path.")
            native = Path("//" + unquote(parts.netloc) + url2pathname(parts.path))
        else:
            native = Path(url2pathname(parts.path))
        if not native.is_absolute() or "\0" in str(native):
            raise ClangdProtocolError("clangd returned a non-absolute file location.")
        return self.workspace.workspace_path(native.resolve())

    def location(self, value: JsonValue) -> Location:
        data = object_value(value)
        if "targetUri" in data:
            return Location(
                path=self.path(string(data["targetUri"])),
                range=source_range(data.get("targetSelectionRange", data.get("targetRange"))),
            )
        return Location(
            path=self.path(string(data.get("uri"))),
            range=source_range(data.get("range")),
        )

    def diagnostic(self, value: JsonValue) -> Diagnostic:
        data = object_value(value)
        levels = {1: "error", 2: "warning", 3: "information", 4: "hint"}
        severity = data.get("severity")
        if severity is not None and (type(severity) is not int or severity not in levels):
            raise ClangdProtocolError("Invalid diagnostic severity from clangd.")
        related = []
        for item in list_value(data.get("relatedInformation", [])):
            entry = object_value(item)
            location = self.location(entry.get("location"))
            if location.path.area != "root":
                related.append(
                    RelatedDiagnostic(
                        location=location,
                        message=string(entry.get("message")),
                    ),
                )
        try:
            return Diagnostic(
                range=source_range(data.get("range")),
                message=string(data.get("message")),
                severity=levels.get(severity),
                code=data.get("code"),
                source=data.get("source"),
                tags=data.get("tags", []),
                related=related,
            )
        except ValidationError as error:
            raise ClangdProtocolError("Invalid diagnostic from clangd.") from error

    async def receive(self) -> None:
        failure: ClangdError = ClangdSessionError("clangd connection closed.")
        try:
            async for message in self.connection.messages():
                if isinstance(message, (LspResponse, LspErrorResponse)):
                    future = self.pending.get(message.id)
                    if future is None or future.done():
                        continue
                    if isinstance(message, LspErrorResponse):
                        future.set_exception(
                            ClangdRequestError(
                                self.methods[message.id],
                                message.code,
                                message.message,
                            ),
                        )
                    else:
                        future.set_result(message.result)
                elif isinstance(message, LspRequest):
                    await self.answer(message)
                elif message.method == "textDocument/publishDiagnostics":
                    data = object_value(message.params)
                    uri = self.uri(self.path(string(data.get("uri"))))
                    document = self.documents.get(uri)
                    if document is None or document.path.area == "root":
                        continue
                    version = data.get("version")
                    # Never label an unversioned or older notification as current.
                    if type(version) is not int or version != document.version:
                        continue
                    diagnostics = [
                        self.diagnostic(item)
                        for item in list_value(data.get("diagnostics"))
                    ]
                    async with self.condition:
                        document.diagnostics = diagnostics
                        document.diagnostics_version = version
                        self.condition.notify_all()
        except asyncio.CancelledError:
            raise
        except ClangdError as error:
            failure = error
        except (ToolchainError, OSError, ValueError) as error:
            failure = ClangdSessionError("clangd connection failed.")
            failure.__cause__ = error
        except Exception as error:
            failure = ClangdSessionError("clangd response processing failed.")
            failure.__cause__ = error
        finally:
            self.failure = failure
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(failure)
            async with self.condition:
                self.condition.notify_all()
            await self.connection.close()

    async def answer(self, request: LspRequest) -> None:
        if request.method == "workspace/configuration":
            items = list_value(object_value(request.params).get("items"))
            await self.connection.send(LspResponse(request.id, [None for _ in items]))
        elif request.method == "workspace/workspaceFolders":
            await self.connection.send(
                LspResponse(
                    request.id,
                    [{"uri": self.workspace.root.as_uri(), "name": self.workspace.root.name}],
                ),
            )
        elif request.method == "workspace/applyEdit":
            await self.connection.send(
                LspResponse(
                    request.id,
                    {"applied": False, "failureReason": "Language analysis is read-only."},
                ),
            )
        else:
            await self.connection.send(
                LspErrorResponse(request.id, -32601, "Unsupported client request."),
            )

    async def initialize(
        self,
        *,
        on_progress: Progress | None = None,
        timeout: float = 30.0,
    ) -> None:
        if self.reader is not None:
            raise ClangdSessionError("A clangd session can only be initialized once.")
        self.reader = asyncio.create_task(self.receive())
        result = object_value(
            await self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": self.workspace.root.as_uri(),
                    "capabilities": {
                        "general": {"positionEncodings": ["utf-32"]},
                        "offsetEncoding": ["utf-32"],
                        "workspace": {
                            "configuration": True,
                            "workspaceFolders": True,
                            "applyEdit": False,
                        },
                        "textDocument": {
                            "publishDiagnostics": {
                                "versionSupport": True,
                                "relatedInformation": True,
                                "tagSupport": {"valueSet": [1, 2]},
                            },
                            "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                            "hover": {"contentFormat": ["markdown", "plaintext"]},
                            "semanticTokens": {
                                "requests": {"full": True},
                                "tokenTypes": [
                                    "namespace",
                                    "type",
                                    "class",
                                    "enum",
                                    "interface",
                                    "struct",
                                    "typeParameter",
                                    "parameter",
                                    "variable",
                                    "property",
                                    "enumMember",
                                    "event",
                                    "function",
                                    "method",
                                    "macro",
                                    "keyword",
                                    "modifier",
                                    "comment",
                                    "string",
                                    "number",
                                    "regexp",
                                    "operator",
                                    "decorator",
                                ],
                                "tokenModifiers": [
                                    "declaration",
                                    "definition",
                                    "readonly",
                                    "static",
                                    "deprecated",
                                    "abstract",
                                    "async",
                                    "modification",
                                    "documentation",
                                    "defaultLibrary",
                                ],
                                "formats": ["relative"],
                            },
                        },
                    },
                },
                on_progress=on_progress,
                timeout=timeout,
            ),
        )
        self.capabilities = object_value(result.get("capabilities"))
        encoding = self.capabilities.get(
            "positionEncoding",
            result.get("offsetEncoding", "utf-16"),
        )
        if encoding != "utf-32":
            raise ClangdProtocolError(
                "This clangd must support UTF-32 positions, including external locations.",
            )
        await self.notify("initialized", {})
        self.initialized = True

    async def request(
        self,
        method: str,
        params: JsonValue,
        *,
        on_progress: Progress | None = None,
        timeout: float = 30.0,
    ) -> JsonValue:
        validate_timeout(timeout)
        self.check_alive()
        if self.reader is None:
            raise ClangdSessionError("clangd message reader has not started.")
        self.next_id += 1
        identifier = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        self.methods[identifier] = method
        try:
            async with asyncio.timeout(timeout):
                await self.report(on_progress, f"Starting {method}")
                await self.connection.send(LspRequest(identifier, method, params))
                result = await future
                await self.report(on_progress, f"Completed {method}")
                return result
        except (TimeoutError, asyncio.CancelledError) as error:
            with suppress(ToolchainError):
                await self.connection.send(
                    LspNotification("$/cancelRequest", {"id": identifier}),
                )
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ClangdTimeoutError(f"{method} exceeded its timeout.") from error
        except ToolchainError as error:
            raise ClangdSessionError("Cannot communicate with clangd.") from error
        finally:
            self.pending.pop(identifier, None)
            self.methods.pop(identifier, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def notify(self, method: str, params: JsonValue) -> None:
        self.check_alive()
        try:
            await self.connection.send(LspNotification(method, params))
        except ToolchainError as error:
            raise ClangdSessionError("Cannot synchronize clangd.") from error

    async def synchronize(
        self,
        path: WorkspacePath,
        text: str,
        *,
        on_progress: Progress | None = None,
    ) -> int:
        self.check_alive()
        uri = self.uri(path)
        document = self.documents.get(uri)
        if document is None:
            document = Document(path, text, version=self.versions.get(uri, 0) + 1)
            self.documents[uri] = document
            language = "c" if path.relative.endswith(".c") else "cpp"
            await self.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": language,
                        "version": document.version,
                        "text": text,
                    },
                },
            )
        elif document.text != text:
            document.version += 1
            document.text = text
            document.diagnostics_version = None
            document.diagnostics = []
            await self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": document.version},
                    "contentChanges": [{"text": text}],
                    "wantDiagnostics": True,
                },
            )
        self.versions[uri] = document.version
        async with self.condition:
            self.condition.notify_all()
        await self.report(on_progress, f"Synchronized {path} version {document.version}")
        return document.version

    async def forget(self, path: WorkspacePath) -> None:
        uri = self.uri(path)
        if uri in self.documents:
            await self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})
            self.documents.pop(uri, None)
            async with self.condition:
                self.condition.notify_all()

    async def diagnostics(
        self,
        path: WorkspacePath,
        version: int,
        *,
        on_progress: Progress | None = None,
        timeout: float = 30.0,
    ) -> list[Diagnostic]:
        validate_timeout(timeout)
        uri = self.uri(path)
        try:
            async with asyncio.timeout(timeout):
                await self.report(
                    on_progress,
                    f"Waiting for diagnostics of {path} version {version}",
                )
                async with self.condition:
                    while True:
                        self.check_alive()
                        document = self.documents.get(uri)
                        if document is None or document.version != version:
                            raise ClangdStaleResultError("The requested document version is no longer current.")
                        if document.diagnostics_version == version:
                            result = [item.model_copy(deep=True) for item in document.diagnostics]
                            break
                        await self.condition.wait()
        except TimeoutError as error:
            raise ClangdTimeoutError(
                f"No diagnostics for {path} version {version} before the timeout.",
            ) from error
        await self.report(on_progress, f"Received diagnostics for {path}")
        return result

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        if self.closing:
            return
        try:
            if self.initialized and self.failure is None:
                with suppress(ClangdError):
                    await self.request("shutdown", None, timeout=timeout)
                    await self.notify("exit", None)
                    if self.reader is not None:
                        with suppress(TimeoutError):
                            async with asyncio.timeout(timeout):
                                await asyncio.shield(self.reader)
        finally:
            self.closing = True
            if self.reader is not None:
                self.reader.cancel()
                with suppress(asyncio.CancelledError):
                    await self.reader
