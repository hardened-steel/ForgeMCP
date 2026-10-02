"""Read-only multi-configuration analysis and immutable workspace enrichment."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from pydantic import BaseModel, JsonValue, ValidationError

from forgemcp.cmake.errors import CMakeError
from forgemcp.cmake.service import CMakeService, CompilationContext
from forgemcp.process.errors import ProcessError
from forgemcp.completion import Complete
from forgemcp.progress import Progress, progress
from forgemcp.toolchain.errors import ToolchainError
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.tools import clangd
from forgemcp.workspace.errors import WorkspaceError
from forgemcp.workspace.extensions import ExtensionContext, ExtensionOutput, ExtensionResource
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import WorkspaceService

from .errors import (
    ClangdError,
    ClangdProtocolError,
    ClangdSessionError,
    ClangdStaleResultError,
    ClangdTimeoutError,
)
from .models import (
    Diagnostic,
    DocumentSymbol,
    HighlightSpan,
    Hover,
    HoverText,
    Location,
    Position,
    SourceRange,
    WorkspaceSymbol,
)
from .session import (
    ClangdSession,
    integer,
    list_value,
    lsp_position,
    object_value,
    source_range,
    string,
    validate_timeout,
)


class DiagnosticsResult(BaseModel):
    configurations: list[str]
    path: WorkspacePath
    diagnostics: list[Diagnostic]


class HoverResult(BaseModel):
    configurations: list[str]
    path: WorkspacePath
    position: Position
    hover: Hover | None


class DefinitionResult(BaseModel):
    configurations: list[str]
    locations: list[Location]


class ReferencesResult(BaseModel):
    configurations: list[str]
    locations: list[Location]


class DocumentSymbolsResult(BaseModel):
    configurations: list[str]
    path: WorkspacePath
    symbols: list[DocumentSymbol]


class WorkspaceSymbolsResult(BaseModel):
    configurations: list[str]
    symbols: list[WorkspaceSymbol]


class HighlightingResult(BaseModel):
    configurations: list[str]
    path: WorkspacePath
    spans: list[HighlightSpan]


class FileAnalysis(BaseModel):
    path: WorkspacePath
    diagnostics: list[DiagnosticsResult]
    highlighting: list[HighlightingResult]


class ClangdResource(BaseModel):
    version: Literal[1] = 1
    files: list[FileAnalysis]


@dataclass
class RunningSession:
    session: ClangdSession
    lifetime: AsyncExitStack
    fingerprint: str
    revision: int


def group_results[T: BaseModel](results: list[T]) -> list[T]:
    """Coalesce equal complete answers, retaining configuration provenance."""
    grouped: dict[str, T] = {}
    for result in results:
        key = json.dumps(
            result.model_dump(mode="json", exclude={"configurations"}),
            sort_keys=True,
            ensure_ascii=False,
        )
        if key in grouped:
            getattr(grouped[key], "configurations").extend(getattr(result, "configurations"))
        else:
            grouped[key] = result.model_copy(deep=True)
    return list(grouped.values())


class ClangdService:
    """Analyze C/C++ files in CMake contexts with compilation databases and clangd.

    Empty configuration selections mean all available contexts. Results describe
    one workspace snapshot; diagnostics outside project/storage are excluded.
    Symbol locations may use root/ without granting access to external files.
    """

    SOURCE_EXTENSIONS = frozenset(
        (
            ".c",
            ".h",
            ".cc",
            ".hh",
            ".cpp",
            ".hpp",
            ".cxx",
            ".hxx",
            ".c++",
            ".h++",
            ".ipp",
            ".tpp",
        ),
    )
    EXTENSION_TOOLS = (
        "workspace_read_file",
        "workspace_write_file",
        "workspace_edit_file",
        "workspace_move",
        "workspace_delete",
    )

    def __init__(
        self,
        workspace: WorkspaceService,
        toolchains: ToolchainService,
        cmake: CMakeService,
        *,
        progress_interval: float = 1.0,
    ) -> None:
        self.workspace = workspace
        self.toolchains = toolchains
        self.cmake = cmake
        self.progress_interval = progress_interval
        self.sessions: dict[str, RunningSession] = {}
        self.lock = asyncio.Lock()
        self.closed = False

    async def configurations(self) -> list[CompilationContext]:
        try:
            contexts = await self.cmake.compilation_contexts()
            return [
                context
                for context in contexts
                if self.toolchains.get_tool(context.toolset_id, "clangd") is not None
            ]
        except (CMakeError, ToolchainError, ProcessError, WorkspaceError) as error:
            raise ClangdError("Cannot obtain CMake compilation contexts.") from error

    async def selection(self, configurations: Sequence[str]) -> list[CompilationContext]:
        if self.closed:
            raise ClangdSessionError("The clangd service is closed.")
        contexts = await self.configurations()
        available = {context.id for context in contexts}
        unknown = set(configurations) - available
        if unknown:
            raise ClangdError(f"Unavailable clangd configurations: {', '.join(sorted(unknown))}.")
        selected = [
            context
            for context in contexts
            if not configurations or context.id in configurations
        ]
        if not selected:
            raise ClangdError("No CMake configuration has both a compilation database and clangd.")
        for identifier in tuple(self.sessions):
            if identifier not in available:
                await self.close_session(identifier)
        return selected

    @asynccontextmanager
    async def operation(self, timeout: float) -> AsyncGenerator[None]:
        """Include queuing, synchronization, and all selected contexts in one deadline."""
        validate_timeout(timeout)
        try:
            async with asyncio.timeout(timeout):
                async with self.lock:
                    if self.closed:
                        raise ClangdSessionError("The clangd service is closed.")
                    yield
        except TimeoutError as error:
            raise ClangdTimeoutError("Language analysis exceeded its timeout.") from error
        except WorkspaceError as error:
            raise ClangdError(str(error)) from error

    async def close_session(self, identifier: str) -> None:
        running = self.sessions.pop(identifier, None)
        if running is not None:
            try:
                await running.session.shutdown()
            finally:
                await running.lifetime.aclose()

    async def close(self) -> None:
        async with self.lock:
            self.closed = True
            results = await asyncio.gather(
                *(self.close_session(identifier) for identifier in tuple(self.sessions)),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result

    async def session(
        self,
        context: CompilationContext,
        on_progress: Progress | None,
        timeout: float,
    ) -> ClangdSession:
        tool = self.toolchains.get_tool(context.toolset_id, "clangd")
        if tool is None:
            raise ClangdError(f"Configuration {context.id} has no clangd.")
        methods = cast(clangd.Methods, tool.methods)
        database = self.workspace.resolve_workspace_path(context.compilation_database)
        try:
            fingerprint = hashlib.sha256(database.read_bytes()).hexdigest()
        except OSError as error:
            raise ClangdError("Cannot read the compilation database.") from error
        fingerprint += f"|{context.toolset_id}|{tool.path}|{database}"
        running = self.sessions.get(context.id)
        if running is not None and (
            running.fingerprint != fingerprint
            or running.session.failure is not None
            or running.revision != self.workspace.revision
        ):
            await self.close_session(context.id)
            running = None
        if running is not None:
            return running.session
        lifetime = AsyncExitStack()
        session = None
        revision = self.workspace.revision
        try:
            connection = await lifetime.enter_async_context(
                methods["connect"](self.workspace.root, database.parent),
            )
            session = ClangdSession(connection, self.workspace, context)
            await session.initialize(on_progress=on_progress, timeout=timeout)
            self.verify_revision(revision)
        except BaseException:
            try:
                if session is not None:
                    await session.shutdown()
            finally:
                await lifetime.aclose()
            raise
        self.sessions[context.id] = RunningSession(session, lifetime, fingerprint, revision)
        return session

    def verify_revision(self, revision: int) -> None:
        if self.workspace.revision != revision:
            raise ClangdStaleResultError("Workspace changed during analysis; retry the operation.")

    def text(self, path: WorkspacePath) -> str:
        if path.area == "root":
            raise ClangdError(
                "Analysis inputs must be project/storage files; external locations are read-only results.",
            )
        try:
            text = self.workspace.resolve_workspace_path(path).read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise ClangdError(f"{path}: cannot read UTF-8 text.") from error
        if "\0" in text:
            raise ClangdError(f"{path}: expected UTF-8 text, found a binary file.")
        return text

    @staticmethod
    def require_capability(session: ClangdSession, name: str) -> None:
        value = session.capabilities.get(name)
        if value is None or value is False:
            raise ClangdError(f"clangd does not support {name}.")

    async def collect[T: BaseModel](
        self,
        contexts: Sequence[CompilationContext],
        operation: Callable[[ClangdSession], Awaitable[T]],
        *,
        on_progress: Progress | None,
        timeout: float,
    ) -> list[T]:
        """Run contexts independently, wait for cleanup, then fail without partial output."""
        async def run(context: CompilationContext) -> T:
            try:
                session = await self.session(context, on_progress, timeout)
                return await operation(session)
            except asyncio.CancelledError:
                await self.close_session(context.id)
                raise
            except (ClangdError, ToolchainError, ProcessError, WorkspaceError) as error:
                await self.close_session(context.id)
                raise ClangdError(f"{context.id}: {error}") from error
            except (ValidationError, KeyError, TypeError, ValueError) as error:
                await self.close_session(context.id)
                raise ClangdProtocolError(f"{context.id}: invalid language-server result.") from error

        results = await asyncio.gather(
            *(run(context) for context in contexts),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return group_results(cast(list[T], results))

    async def file_operation[T: BaseModel](
        self,
        path: WorkspacePath,
        configurations: Sequence[str],
        operation: Callable[[ClangdSession, int], Awaitable[T]],
        *,
        on_progress: Progress | None,
        timeout: float,
    ) -> list[T]:
        async with self.operation(timeout):
            contexts = await self.selection(configurations)
            revision = self.workspace.revision
            text = self.text(path)

            async def run(session: ClangdSession) -> T:
                version = await session.synchronize(
                    path,
                    text,
                    on_progress=on_progress,
                )
                return await operation(session, version)

            result = await self.collect(
                contexts,
                run,
                on_progress=on_progress,
                timeout=timeout,
            )
            self.verify_revision(revision)
            return result

    async def diagnostics(
        self,
        path: WorkspacePath,
        *,
        configurations: Sequence[str] = (),
        on_progress: Progress | None = None,
        timeout: float = 30.0,
    ) -> list[DiagnosticsResult]:
        async def run(session: ClangdSession, version: int) -> DiagnosticsResult:
            return DiagnosticsResult(
                configurations=[session.configuration.id],
                path=path,
                diagnostics=await session.diagnostics(
                    path,
                    version,
                    on_progress=on_progress,
                    timeout=timeout,
                ),
            )

        return await self.file_operation(
            path,
            configurations,
            run,
            on_progress=on_progress,
            timeout=timeout,
        )

    def position_params(
        self,
        session: ClangdSession,
        path: WorkspacePath,
        position: Position,
    ) -> dict[str, JsonValue]:
        uri = session.uri(path)
        lines = session.documents[uri].text.split("\n")
        if position.line > len(lines) or position.character > len(lines[position.line - 1].removesuffix("\r")):
            raise ClangdError("Position is outside the document.")
        return {"textDocument": {"uri": uri}, "position": lsp_position(position)}

    async def hover(
        self,
        path: WorkspacePath,
        position: Position,
        *,
        configurations: Sequence[str] = (),
        on_progress: Progress | None = None,
        timeout: float = 30.0,
    ) -> list[HoverResult]:
        async def run(session: ClangdSession, version: int) -> HoverResult:
            self.require_capability(session, "hoverProvider")
            raw = await session.request(
                "textDocument/hover",
                self.position_params(session, path, position),
                on_progress=on_progress,
                timeout=timeout,
            )
            hover = None
            if raw is not None:
                data = object_value(raw)
                values = data.get("contents")
                values = values if isinstance(values, list) else [values]
                contents = []
                for value in values:
                    if isinstance(value, str):
                        contents.append(HoverText(kind="markdown", text=value))
                    else:
                        item = object_value(value)
                        contents.append(
                            HoverText(
                                kind="code" if "language" in item else item.get("kind"),
                                text=string(item.get("value")),
                                language=item.get("language"),
                            ),
                        )
                hover = Hover(
                    contents=contents,
                    range=source_range(data["range"]) if "range" in data else None,
                )
            return HoverResult(
                configurations=[session.configuration.id],
                path=path,
                position=position,
                hover=hover,
            )

        return await self.file_operation(
            path,
            configurations,
            run,
            on_progress=on_progress,
            timeout=timeout,
        )

    async def locations(
        self,
        session: ClangdSession,
        method: str,
        params: dict[str, JsonValue],
        on_progress: Progress | None,
        timeout: float,
    ) -> list[Location]:
        raw = await session.request(method, params, on_progress=on_progress, timeout=timeout)
        if raw is None:
            return []
        values = raw if isinstance(raw, list) else [raw]
        locations = [session.location(value) for value in values]
        return sorted(
            locations,
            key=lambda item: (
                str(item.path),
                item.range.start.line,
                item.range.start.character,
                item.range.end.line,
                item.range.end.character,
            ),
        )

    async def definition(
        self,
        path: WorkspacePath,
        position: Position,
        *,
        configurations: Sequence[str] = (),
        on_progress: Progress | None = None,
        timeout: float = 30.0,
    ) -> list[DefinitionResult]:
        async def run(session: ClangdSession, version: int) -> DefinitionResult:
            self.require_capability(session, "definitionProvider")
            return DefinitionResult(
                configurations=[session.configuration.id],
                locations=await self.locations(
                    session,
                    "textDocument/definition",
                    self.position_params(session, path, position),
                    on_progress,
                    timeout,
                ),
            )

        return await self.file_operation(
            path,
            configurations,
            run,
            on_progress=on_progress,
            timeout=timeout,
        )

    async def references(
        self,
        path: WorkspacePath,
        position: Position,
        *,
        configurations: Sequence[str] = (),
        include_declaration: bool = True,
        on_progress: Progress | None = None,
        timeout: float = 30.0,
    ) -> list[ReferencesResult]:
        async def run(session: ClangdSession, version: int) -> ReferencesResult:
            self.require_capability(session, "referencesProvider")
            params = self.position_params(session, path, position)
            params["context"] = {"includeDeclaration": include_declaration}
            return ReferencesResult(
                configurations=[session.configuration.id],
                locations=await self.locations(
                    session,
                    "textDocument/references",
                    params,
                    on_progress,
                    timeout,
                ),
            )

        return await self.file_operation(
            path,
            configurations,
            run,
            on_progress=on_progress,
            timeout=timeout,
        )

    def document_symbol(self, value: JsonValue) -> DocumentSymbol:
        data = object_value(value)
        return DocumentSymbol(
            name=string(data.get("name")),
            kind=integer(data.get("kind")),
            range=source_range(data.get("range")),
            selection_range=source_range(data.get("selectionRange")),
            detail=data.get("detail"),
            tags=data.get("tags", [1] if data.get("deprecated") else []),
            children=[self.document_symbol(item) for item in list_value(data.get("children", []))],
        )

    async def document_symbols(
        self,
        path: WorkspacePath,
        *,
        configurations: Sequence[str] = (),
        on_progress: Progress | None = None,
        timeout: float = 30.0,
    ) -> list[DocumentSymbolsResult]:
        async def run(session: ClangdSession, version: int) -> DocumentSymbolsResult:
            self.require_capability(session, "documentSymbolProvider")
            raw = await session.request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": session.uri(path)}},
                on_progress=on_progress,
                timeout=timeout,
            )
            symbols = []
            for value in list_value([] if raw is None else raw):
                data = object_value(value)
                if "location" in data:
                    location = session.location(data["location"])
                    if location.path != path:
                        raise ClangdProtocolError("Document symbol points to another file.")
                    symbols.append(
                        DocumentSymbol(
                            name=string(data.get("name")),
                            kind=integer(data.get("kind")),
                            range=location.range,
                            selection_range=location.range,
                            tags=data.get("tags", []),
                        ),
                    )
                else:
                    symbols.append(self.document_symbol(value))
            return DocumentSymbolsResult(
                configurations=[session.configuration.id],
                path=path,
                symbols=symbols,
            )

        return await self.file_operation(
            path,
            configurations,
            run,
            on_progress=on_progress,
            timeout=timeout,
        )

    async def workspace_symbols(
        self,
        query: str,
        *,
        configurations: Sequence[str] = (),
        on_progress: Progress | None = None,
        timeout: float = 30.0,
    ) -> list[WorkspaceSymbolsResult]:
        validate_timeout(timeout)

        async def run(session: ClangdSession) -> WorkspaceSymbolsResult:
            self.require_capability(session, "workspaceSymbolProvider")
            raw = await session.request(
                "workspace/symbol",
                {"query": query},
                on_progress=on_progress,
                timeout=timeout,
            )
            symbols = []
            for value in list_value([] if raw is None else raw):
                data = object_value(value)
                symbols.append(
                    WorkspaceSymbol(
                        name=string(data.get("name")),
                        kind=integer(data.get("kind")),
                        location=session.location(data.get("location")),
                        container_name=data.get("containerName"),
                        tags=data.get("tags", []),
                    ),
                )
            return WorkspaceSymbolsResult(configurations=[session.configuration.id], symbols=symbols)

        async with self.operation(timeout):
            contexts = await self.selection(configurations)
            revision = self.workspace.revision
            result = await self.collect(
                contexts,
                run,
                on_progress=on_progress,
                timeout=timeout,
            )
            self.verify_revision(revision)
            return result

    async def highlighting(
        self,
        session: ClangdSession,
        path: WorkspacePath,
        *,
        on_progress: Progress | None,
        timeout: float,
    ) -> HighlightingResult:
        self.require_capability(session, "semanticTokensProvider")
        provider = object_value(session.capabilities["semanticTokensProvider"])
        if provider.get("full") is None or provider.get("full") is False:
            raise ClangdError("clangd does not support full semantic tokens.")
        legend = object_value(provider.get("legend"))
        kinds = [string(item) for item in list_value(legend.get("tokenTypes"))]
        modifiers = [string(item) for item in list_value(legend.get("tokenModifiers"))]
        raw = await session.request(
            "textDocument/semanticTokens/full",
            {"textDocument": {"uri": session.uri(path)}},
            on_progress=on_progress,
            timeout=timeout,
        )
        data = [] if raw is None else list_value(object_value(raw).get("data"))
        if len(data) % 5:
            raise ClangdProtocolError("Invalid semantic-token array length.")
        line = 1
        character = 0
        spans = []
        lines = session.documents[session.uri(path)].text.split("\n")
        for index in range(0, len(data), 5):
            delta, offset, length, kind, flags = [integer(item) for item in data[index:index + 5]]
            line += delta
            character = offset if delta else character + offset
            if (
                kind >= len(kinds)
                or flags >> len(modifiers)
                or length == 0
                or line > len(lines)
                or character + length > len(lines[line - 1].removesuffix("\r"))
            ):
                raise ClangdProtocolError("Invalid semantic-token coordinates or legend index.")
            spans.append(
                HighlightSpan(
                    range=SourceRange(
                        start=Position(line=line, character=character),
                        end=Position(line=line, character=character + length),
                    ),
                    kind=kinds[kind],
                    modifiers=[name for bit, name in enumerate(modifiers) if flags & (1 << bit)],
                ),
            )
        return HighlightingResult(
            configurations=[session.configuration.id],
            path=path,
            spans=spans,
        )

    async def workspace_extension(self, context: ExtensionContext) -> ExtensionOutput | None:
        if context.tool_name not in self.EXTENSION_TOOLS:
            return None
        async with self.operation(30.0):
            revision = self.workspace.revision
            if context.change is not None and context.change.revision not in (None, revision):
                raise ClangdStaleResultError("This workspace change has already been superseded.")
            texts = dict(context.texts)
            if context.change is not None:
                texts[context.change.path] = context.change.after
            for path, text in texts.items():
                if path.area != "root" and self.text(path) != text:
                    raise ClangdStaleResultError("The operation's file snapshot is no longer current.")
            try:
                async with asyncio.timeout(30):
                    if context.tool_name != "workspace_read_file":
                        await self.synchronize_change(context, revision)
                    paths = [
                        path
                        for path in context.paths
                        if path.area != "root"
                        and self.workspace.resolve_workspace_path(path).suffix.lower() in self.SOURCE_EXTENSIONS
                        and path in texts
                    ]
                    if not paths:
                        return None
                    contexts = await self.configurations()
                    if not contexts:
                        return None
                    files = []
                    for path in dict.fromkeys(paths):
                        async def run(session: ClangdSession) -> FileAnalysis:
                            version = await session.synchronize(
                                path,
                                texts[path],
                                on_progress=context.on_progress,
                            )
                            diagnostics = await session.diagnostics(
                                path,
                                version,
                                on_progress=context.on_progress,
                            )
                            highlighting = await self.highlighting(
                                session,
                                path,
                                on_progress=context.on_progress,
                                timeout=30.0,
                            )
                            return FileAnalysis(
                                path=path,
                                diagnostics=[
                                    DiagnosticsResult(
                                        configurations=[session.configuration.id],
                                        path=path,
                                        diagnostics=diagnostics,
                                    ),
                                ],
                                highlighting=[highlighting],
                            )

                        # FileAnalysis is an internal aggregate, not a grouped
                        # tool result. Collect the two concrete answer types below.
                        answers = []
                        for selected in contexts:
                            session = await self.session(selected, context.on_progress, 30.0)
                            answers.append(await run(session))
                        files.append(
                            FileAnalysis(
                                path=path,
                                diagnostics=group_results(
                                    [item for answer in answers for item in answer.diagnostics],
                                ),
                                highlighting=group_results(
                                    [item for answer in answers for item in answer.highlighting],
                                ),
                            ),
                        )
                    self.verify_revision(revision)
                    return ExtensionOutput(
                        resource=ExtensionResource(
                            name="clangd.json",
                            mime_type="application/json",
                            text=ClangdResource(files=files).model_dump_json(),
                        ),
                        metadata={"version": 1},
                    )
            except TimeoutError as error:
                raise ClangdTimeoutError("Workspace analysis exceeded its timeout.") from error
            finally:
                # An overlapping mutation must force fresh state next time.
                if self.workspace.revision != revision:
                    for running in self.sessions.values():
                        running.revision = -1

    async def synchronize_change(self, context: ExtensionContext, revision: int) -> None:
        changes = []
        for index, path in enumerate(context.paths):
            if path.area == "root":
                continue
            removed = context.tool_name == "workspace_delete" or (
                context.tool_name == "workspace_move" and index == 0
            )
            created = context.tool_name == "workspace_move" and index == 1
            if context.tool_name == "workspace_write_file":
                created = getattr(context.result, "action", None) == "created"
            changes.append(
                {
                    "uri": self.workspace.resolve_workspace_path(path).as_uri(),
                    "type": 3 if removed else 1 if created else 2,
                },
            )
        for identifier, running in tuple(self.sessions.items()):
            try:
                await running.session.notify("workspace/didChangeWatchedFiles", {"changes": changes})
                for document in tuple(running.session.documents.values()):
                    try:
                        text = self.text(document.path)
                    except (WorkspaceError, ClangdError):
                        await running.session.forget(document.path)
                    else:
                        await running.session.synchronize(
                            document.path,
                            text,
                            on_progress=context.on_progress,
                        )
                self.verify_revision(revision)
                running.revision = revision
            except BaseException:
                await self.close_session(identifier)
                raise

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        """Expose read-only analysis; widget registration is deferred."""
        read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False)

        async def invoke[T](
            operation: Callable[..., Awaitable[T]],
            ctx: Context,
            **arguments: Any,
        ) -> T:
            report = progress(ctx, interval=self.progress_interval)
            updates = 0

            async def status(message: str) -> None:
                nonlocal updates
                updates += 1
                await report(updates, message=message)

            await report(0, message="Starting clangd analysis")
            try:
                result = await operation(on_progress=status, **arguments)
            except (ClangdError, WorkspaceError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            await status("Completed clangd analysis")
            return result

        @mcp.tool(icons=[WorkspaceService.FILE_ICON.icon], annotations=read_only)
        async def clangd_configurations(ctx: Context) -> list[CompilationContext]:
            """List CMake contexts with an existing compilation database and clangd."""
            report = progress(ctx, interval=self.progress_interval)
            await report(0, message="Finding clangd configurations")
            try:
                result = await self.configurations()
            except ClangdError as error:
                raise ToolError(str(error)) from error
            await report(1, total=1, message="Completed configuration discovery")
            return result

        @mcp.tool(icons=[WorkspaceService.FILE_ICON.icon], annotations=read_only)
        async def clangd_diagnostics(
            path: WorkspacePath,
            ctx: Context,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[DiagnosticsResult]:
            """Get diagnostics for a managed file in selected configurations. Empty configurations selects all."""
            return await invoke(
                self.diagnostics,
                ctx,
                path=path,
                configurations=configurations,
                timeout=timeout,
            )

        @mcp.tool(icons=[WorkspaceService.FILE_ICON.icon], annotations=read_only)
        async def clangd_hover(
            path: WorkspacePath,
            ctx: Context,
            position: Position,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[HoverResult]:
            """Describe the symbol at a one-based line and zero-based code-point position. Empty configurations selects all."""
            return await invoke(
                self.hover,
                ctx,
                path=path,
                position=position,
                configurations=configurations,
                timeout=timeout,
            )

        @mcp.tool(icons=[WorkspaceService.FILE_ICON.icon], annotations=read_only)
        async def clangd_definition(
            path: WorkspacePath,
            ctx: Context,
            position: Position,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[DefinitionResult]:
            """Find symbol definitions; external locations use root/. Empty configurations selects all."""
            return await invoke(
                self.definition,
                ctx,
                path=path,
                position=position,
                configurations=configurations,
                timeout=timeout,
            )

        @mcp.tool(icons=[WorkspaceService.FILE_ICON.icon], annotations=read_only)
        async def clangd_references(
            path: WorkspacePath,
            ctx: Context,
            position: Position,
            include_declaration: bool = True,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[ReferencesResult]:
            """Find references to the symbol at the supplied position. Empty configurations selects all."""
            return await invoke(
                self.references,
                ctx,
                path=path,
                position=position,
                include_declaration=include_declaration,
                configurations=configurations,
                timeout=timeout,
            )

        @mcp.tool(icons=[WorkspaceService.FILE_ICON.icon], annotations=read_only)
        async def clangd_document_symbols(
            path: WorkspacePath,
            ctx: Context,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[DocumentSymbolsResult]:
            """List the symbols declared in a managed file. Empty configurations selects all."""
            return await invoke(
                self.document_symbols,
                ctx,
                path=path,
                configurations=configurations,
                timeout=timeout,
            )

        @mcp.tool(icons=[WorkspaceService.FILE_ICON.icon], annotations=read_only)
        async def clangd_workspace_symbols(
            query: str,
            ctx: Context,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[WorkspaceSymbolsResult]:
            """Find workspace symbols by name query. Empty configurations selects all."""
            return await invoke(
                self.workspace_symbols,
                ctx,
                query=query,
                configurations=configurations,
                timeout=timeout,
            )
