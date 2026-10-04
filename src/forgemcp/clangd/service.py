"""Read-only multi-configuration analysis and immutable workspace enrichment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, JsonValue, ValidationError

from forgemcp.assets import IconFile, Widget
from forgemcp.cmake.service import CMakeService, CompilationContext
from forgemcp.completion import Complete
from forgemcp.process.errors import ProcessError
from forgemcp.progress import Progress, progress
from forgemcp.toolchain.errors import ToolchainError
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.tools import clangd
from forgemcp.workspace.errors import WorkspaceError
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import (
    FileContent,
    FileEditResult,
    FileWriteResult,
    PathOperationResult,
    ReadConfirmation,
    ResultProvider,
    WorkspaceService,
)

from .errors import (
    ClangdError,
    ClangdProtocolError,
    ClangdSessionError,
    ClangdTimeoutError,
)
from .models import (
    Diagnostic,
    DocumentSymbol,
    HighlightSpan,
    Hover,
    HoverText,
    Location,
    NavigationLocation,
    Position,
    SourceExcerpt,
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
    """Diagnostics for one file with the configurations that produced them."""

    configurations: list[str]
    path: WorkspacePath
    diagnostics: list[Diagnostic]


class HoverResult(BaseModel):
    """A hover answer at a source position with configuration provenance."""

    configurations: list[str]
    path: WorkspacePath
    position: Position
    hover: Hover | None


class DefinitionResult(BaseModel):
    """Definition targets shared by the listed configurations."""

    configurations: list[str]
    locations: list[NavigationLocation]


class ReferencesResult(BaseModel):
    """Reference targets shared by the listed configurations."""

    configurations: list[str]
    locations: list[NavigationLocation]


class DocumentSymbolsResult(BaseModel):
    """A file's symbol tree with configuration provenance."""

    configurations: list[str]
    path: WorkspacePath
    symbols: list[DocumentSymbol]


class WorkspaceSymbolsResult(BaseModel):
    """Matching workspace symbols with configuration provenance."""

    configurations: list[str]
    symbols: list[WorkspaceSymbol]


class HighlightingResult(BaseModel):
    """Semantic spans for a file with configuration provenance."""

    configurations: list[str]
    path: WorkspacePath
    spans: list[HighlightSpan]


class FileAnalysis(BaseModel):
    """Saved diagnostic and highlighting answers for one file."""

    path: WorkspacePath
    diagnostics: list[DiagnosticsResult]
    highlighting: list[HighlightingResult]


class ClangdResource(BaseModel):
    """The versioned payload of an immutable workspace analysis resource."""

    version: Literal[1] = 1
    files: list[FileAnalysis]


@dataclass
class RunningSession:
    """A retained LSP session with its owned lifetime and configuration fingerprint."""

    session: ClangdSession
    lifetime: AsyncExitStack
    fingerprint: str


@dataclass(frozen=True)
class WorkspaceContext:
    """The managed paths captured before a workspace operation."""

    paths: tuple[WorkspacePath, ...]


@dataclass(frozen=True)
class FileChange:
    """A managed path and its LSP create, change, or delete event kind."""

    path: WorkspacePath
    kind: Literal[1, 2, 3]


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


class ClangdService(ResultProvider[WorkspaceContext]):
    """Clangd: read-only semantic C/C++ analysis and navigation across CMake contexts.

    Each context requires a compilation database and clangd in its toolset.
    Omitted or empty configuration selections use all available contexts; equal
    answers are grouped by configuration IDs. Positions use one-based lines and
    zero-based Unicode code-point characters.

    Results and linked analysis resources are immutable snapshots. Workspace
    mutations synchronize retained sessions; external edits are not watched.
    Diagnostics outside project/storage are excluded; symbol locations may use
    root/... without granting file access.
    """

    WIDGET = Widget("assets/clangd-result.html")
    ICON = IconFile("icons/clangd.svg")
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
    ANALYSIS_TIMEOUT = 30.0
    PROVIDER_TOOLS = (
        "workspace_read_file",
        "workspace_write_file",
        "workspace_edit_file",
        "workspace_move",
        "workspace_delete",
        "workspace_mkdir",
    )

    def __init__(
        self,
        workspace: WorkspaceService,
        toolchains: ToolchainService,
        cmake: CMakeService,
        *,
        progress_interval: float = 1.0,
    ) -> None:
        """Bind analysis dependencies and initialize session, subscription, and locking state."""
        self.workspace = workspace
        self.toolchains = toolchains
        self.cmake = cmake
        self.progress_interval = progress_interval
        self.sessions: dict[str, RunningSession] = {}
        self.lock = asyncio.Lock()
        self.closed = False
        self.contexts: dict[str, CompilationContext] = {}
        self.configuration_task: asyncio.Task[None] | None = None
        self.subscription_failure: ClangdError | None = None

    async def initialize(self) -> None:
        """Subscribe to CMake and start every currently available clangd session."""
        if self.closed:
            raise ClangdSessionError("The clangd service is closed.")
        if self.configuration_task is not None:
            return
        updates = self.cmake.configuration_updates()
        try:
            contexts = await anext(updates)
            async with self.lock:
                await self.update_configurations(contexts)
            self.configuration_task = asyncio.create_task(
                self.watch_configurations(updates),
            )
        except BaseException:
            await updates.aclose()
            raise

    async def watch_configurations(
        self,
        updates: AsyncGenerator[list[CompilationContext], None],
    ) -> None:
        """Apply subscribed CMake snapshots and retain a background subscription failure."""
        try:
            async for contexts in updates:
                async with self.lock:
                    if self.closed:
                        return
                    await self.update_configurations(contexts)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.subscription_failure = ClangdSessionError("CMake configuration subscription failed.")
            self.subscription_failure.__cause__ = error
            logging.getLogger(__name__).warning("Clangd configuration subscription failed")
        finally:
            await updates.aclose()

    async def update_configurations(self, contexts: Sequence[CompilationContext]) -> None:
        """Apply a CMake snapshot while holding the analysis lock."""
        available = {}
        for context in contexts:
            try:
                tool = self.toolchains.get_tool(context.toolset_id, "clangd")
                database = self.workspace.resolve_workspace_path(context.compilation_database)
                if tool is not None and database.is_file():
                    available[context.id] = context.model_copy(deep=True)
            except (ToolchainError, WorkspaceError, OSError):
                continue
        self.contexts = available
        for identifier in tuple(self.sessions):
            if identifier not in available:
                await self.close_session(identifier)
        for context in available.values():
            try:
                running = self.sessions.get(context.id)
                if running is not None and (
                    running.session.configuration != context
                    or running.fingerprint != self.fingerprint(context)
                    or running.session.failure is not None
                ):
                    await self.close_session(context.id)
                async with asyncio.timeout(self.ANALYSIS_TIMEOUT):
                    await self.session(context, None, self.ANALYSIS_TIMEOUT)
            except (ClangdError, ToolchainError, ProcessError, WorkspaceError):
                logging.getLogger(__name__).warning(
                    "Cannot initialize clangd configuration: %s",
                    context.id,
                )
            except TimeoutError:
                logging.getLogger(__name__).warning(
                    "Clangd initialization timed out: %s",
                    context.id,
                )

    def check_ready(self) -> None:
        """Reject a closed, uninitialized, or failed analysis service."""
        if self.closed:
            raise ClangdSessionError("The clangd service is closed.")
        if self.configuration_task is None:
            raise ClangdSessionError("The clangd service is not initialized.")
        if self.subscription_failure is not None:
            raise self.subscription_failure

    async def configurations(self) -> list[CompilationContext]:
        """Return independent copies of currently usable compilation contexts."""
        self.check_ready()
        return [context.model_copy(deep=True) for context in self.contexts.values()]

    async def selection(self, configurations: Sequence[str]) -> list[CompilationContext]:
        """Validate requested configuration IDs, using all contexts for an empty selection."""
        contexts = await self.configurations()
        unknown = set(configurations) - self.contexts.keys()
        if unknown:
            raise ClangdError(f"Unavailable clangd configurations: {', '.join(sorted(unknown))}.")
        selected = [
            context
            for context in contexts
            if not configurations or context.id in configurations
        ]
        if not selected:
            raise ClangdError("No CMake configuration has both a compilation database and clangd.")
        return selected

    @asynccontextmanager
    async def operation(self, timeout: float) -> AsyncGenerator[None]:
        """Include queuing, synchronization, and selected contexts in one deadline."""
        validate_timeout(timeout)
        try:
            async with asyncio.timeout(timeout):
                async with self.lock:
                    self.check_ready()
                    yield
        except TimeoutError as error:
            raise ClangdTimeoutError("Language analysis exceeded its timeout.") from error
        except WorkspaceError as error:
            raise ClangdError(str(error)) from error

    async def close_session(self, identifier: str) -> None:
        """Remove a session, shut it down, and release its owned connection lifetime."""
        running = self.sessions.pop(identifier, None)
        if running is not None:
            try:
                await running.session.shutdown()
            finally:
                await running.lifetime.aclose()

    async def close_sessions(self) -> None:
        """Close all retained sessions and propagate any cleanup failure."""
        results = await asyncio.gather(
            *(self.close_session(identifier) for identifier in tuple(self.sessions)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def close(self) -> None:
        """Stop configuration updates and close all sessions under the analysis lock."""
        self.closed = True
        if self.configuration_task is not None:
            self.configuration_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.configuration_task
        async with self.lock:
            await self.close_sessions()
            self.contexts.clear()

    def fingerprint(self, context: CompilationContext) -> str:
        """Fingerprint the selected clangd executable and compilation database contents."""
        tool = self.toolchains.get_tool(context.toolset_id, "clangd")
        if tool is None:
            raise ClangdError(f"Configuration {context.id} has no clangd.")
        database = self.workspace.resolve_workspace_path(context.compilation_database)
        try:
            digest = hashlib.sha256(database.read_bytes()).hexdigest()
        except OSError as error:
            raise ClangdError("Cannot read the compilation database.") from error
        return f"{digest}|{context.toolset_id}|{tool.path}|{database}"

    async def session(
        self,
        context: CompilationContext,
        on_progress: Progress | None,
        timeout: float,
    ) -> ClangdSession:
        """Reuse a healthy context session or create and initialize its owned connection."""
        running = self.sessions.get(context.id)
        if running is not None and running.session.failure is not None:
            await self.close_session(context.id)
            running = None
        if running is not None:
            return running.session
        tool = self.toolchains.get_tool(context.toolset_id, "clangd")
        if tool is None:
            raise ClangdError(f"Configuration {context.id} has no clangd.")
        methods = cast(clangd.Methods, tool.methods)
        database = self.workspace.resolve_workspace_path(context.compilation_database)
        fingerprint = self.fingerprint(context)
        lifetime = AsyncExitStack()
        session = None
        try:
            connection = await lifetime.enter_async_context(
                methods["connect"](self.workspace.root, database.parent),
            )
            session = ClangdSession(connection, self.workspace, context)
            await session.initialize(on_progress=on_progress, timeout=timeout)
        except BaseException:
            try:
                if session is not None:
                    await session.shutdown()
            finally:
                await lifetime.aclose()
            raise
        self.sessions[context.id] = RunningSession(session, lifetime, fingerprint)
        return session

    def text(self, path: WorkspacePath) -> str:
        """Read managed UTF-8 source and reject external paths or binary text."""
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
        """Reject an analysis operation whose server capability is absent or false."""
        value = session.capabilities.get(name)
        if value is None or value is False:
            raise ClangdError(f"clangd does not support {name}.")

    async def run_context[T](
        self,
        context: CompilationContext,
        operation: Callable[[ClangdSession], Awaitable[T]],
        on_progress: Progress | None,
        timeout: float,
    ) -> T:
        """Run an operation in one context and discard its session on failure or cancellation."""
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

    async def collect[T: BaseModel](
        self,
        contexts: Sequence[CompilationContext],
        operation: Callable[[ClangdSession], Awaitable[T]],
        *,
        on_progress: Progress | None,
        timeout: float,
    ) -> list[T]:
        """Run contexts independently, wait for cleanup, then fail without partial output."""
        results = await asyncio.gather(
            *(
                self.run_context(context, operation, on_progress, timeout)
                for context in contexts
            ),
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
        """Synchronize a source file and collect selected-context answers within one deadline."""
        async with self.operation(timeout):
            contexts = await self.selection(configurations)
            text = self.text(path)

            async def run(session: ClangdSession) -> T:
                """Synchronize the selected session before executing the version-specific
                operation.
                """
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
            return result

    def position_params(
        self,
        session: ClangdSession,
        path: WorkspacePath,
        position: Position,
    ) -> dict[str, JsonValue]:
        """Validate a source position against retained text and build LSP request parameters."""
        uri = session.uri(path)
        lines = session.documents[uri].text.split("\n")
        if position.line > len(lines) or position.character > len(lines[position.line - 1].removesuffix("\r")):
            raise ClangdError("Position is outside the document.")
        return {"textDocument": {"uri": uri}, "position": lsp_position(position)}

    async def locations(
        self,
        session: ClangdSession,
        method: str,
        params: dict[str, JsonValue],
        on_progress: Progress | None,
        timeout: float,
    ) -> list[NavigationLocation]:
        """Request navigation targets, attach saved excerpts, and sort them by source position."""
        raw = await session.request(method, params, on_progress=on_progress, timeout=timeout)
        if raw is None:
            return []
        values = raw if isinstance(raw, list) else [raw]
        locations = [session.location(value) for value in values]
        return sorted(
            self.navigation_locations(locations),
            key=lambda item: (
                str(item.path),
                item.range.start.line,
                item.range.start.character,
                item.range.end.line,
                item.range.end.character,
            ),
        )

    def navigation_locations(
        self,
        locations: Sequence[Location],
    ) -> list[NavigationLocation]:
        """Capture seven source lines per target; never read external locations."""
        sources: dict[WorkspacePath, list[str] | None] = {}
        result = []
        for location in locations:
            preview = None
            if location.path.area != "root":
                if location.path not in sources:
                    try:
                        sources[location.path] = self.text(location.path).splitlines(keepends=True)
                    except (ClangdError, WorkspaceError):
                        sources[location.path] = None
                lines = sources[location.path]
                if lines and location.range.start.line <= len(lines):
                    start = max(1, location.range.start.line - 3)
                    end = min(len(lines), location.range.start.line + 3)
                    preview = SourceExcerpt(
                        start_line=start,
                        text="".join(lines[start - 1:end]),
                    )
            result.append(
                NavigationLocation(
                    path=location.path,
                    range=location.range,
                    preview=preview,
                ),
            )
        return result

    def document_symbol(self, value: JsonValue) -> DocumentSymbol:
        """Decode a symbol and recursively decode its child symbols."""
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

    async def highlighting(
        self,
        session: ClangdSession,
        path: WorkspacePath,
        *,
        on_progress: Progress | None,
        timeout: float,
    ) -> HighlightingResult:
        """Decode full semantic tokens into source spans using the server's token legend."""
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
                or character + length > len(lines[line - 1])
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

    async def begin_workspace(
        self,
        path: WorkspacePath,
        *,
        subtree: bool = False,
    ) -> WorkspaceContext | None:
        """Lock analysis and capture managed paths affected by a workspace operation."""
        if path.area == "root":
            return None
        await self.lock.acquire()
        try:
            self.check_ready()
            paths = [path]
            native = self.workspace.resolve_workspace_path(path)
            if subtree and native.is_dir():
                self.workspace.check_path_links(native, path.area)

                def raise_walk_error(error: OSError) -> None:
                    """Propagate traversal failures instead of returning an incomplete subtree."""
                    raise error

                for directory, directories, files in os.walk(
                    native,
                    followlinks=False,
                    onerror=raise_walk_error,
                ):
                    parent = Path(directory)
                    directories[:] = [
                        name
                        for name in directories
                        if not (parent / name).is_symlink() and not (parent / name).is_junction()
                    ]
                    paths.extend(
                        self.workspace.workspace_path(parent / name)
                        for name in files
                        if not (parent / name).is_symlink() and (parent / name).is_file()
                    )
            return WorkspaceContext(tuple(paths))
        except BaseException:
            try:
                await self.close_sessions()
            finally:
                self.lock.release()
            raise

    async def before_workspace_read_file(
        self,
        call_id: str,
        path: WorkspacePath,
        confirm: ReadConfirmation,
        start_line: int,
        end_line: int | None,
    ) -> WorkspaceContext | None:
        """Capture and lock the file whose read result will receive analysis."""
        return await self.begin_workspace(path)

    async def after_workspace_read_file(
        self,
        call_id: str,
        context: WorkspaceContext,
        result: FileContent,
    ) -> str | None:
        """Save analysis for the completed read and release the analysis lock."""
        return await self.finish_workspace(call_id, context)

    async def before_workspace_write_file(
        self,
        call_id: str,
        path: WorkspacePath,
        text: str,
    ) -> WorkspaceContext | None:
        """Capture and lock the source path before a write."""
        return await self.begin_workspace(path)

    async def after_workspace_write_file(
        self,
        call_id: str,
        context: WorkspaceContext,
        result: FileWriteResult,
    ) -> str | None:
        """Notify creation or modification and save analysis for the written file."""
        return await self.finish_workspace(
            call_id,
            context,
            changes=[FileChange(result.path, 1 if result.action == "created" else 2)],
        )

    async def before_workspace_edit_file(
        self,
        call_id: str,
        path: WorkspacePath,
        old_text: str,
        new_text: str,
        replace_all: bool,
    ) -> WorkspaceContext | None:
        """Capture and lock the source path before an exact edit."""
        return await self.begin_workspace(path)

    async def after_workspace_edit_file(
        self,
        call_id: str,
        context: WorkspaceContext,
        result: FileEditResult,
    ) -> str | None:
        """Notify modification and save analysis for the edited file."""
        return await self.finish_workspace(
            call_id,
            context,
            changes=[FileChange(result.path, 2)],
        )

    async def before_workspace_move(
        self,
        call_id: str,
        source: WorkspacePath,
        destination: WorkspacePath,
    ) -> WorkspaceContext | None:
        """Capture and lock all managed paths under the move source."""
        return await self.begin_workspace(source, subtree=True)

    async def after_workspace_move(
        self,
        call_id: str,
        context: WorkspaceContext,
        result: PathOperationResult,
    ) -> str | None:
        """Notify old and new paths of a move and release the analysis lock."""
        try:
            origin = self.workspace.resolve_workspace_path(context.paths[0])
            destination = self.workspace.resolve_workspace_path(result.path)
            changes = []
            for old in context.paths:
                relative = self.workspace.resolve_workspace_path(old).relative_to(origin)
                new = self.workspace.workspace_path(destination / relative)
                changes.extend([FileChange(old, 3), FileChange(new, 1)])
        except BaseException:
            try:
                await self.close_sessions()
            finally:
                self.lock.release()
            raise
        return await self.finish_workspace(
            call_id,
            context,
            changes=changes,
            paths=(),
        )

    async def before_workspace_delete(
        self,
        call_id: str,
        path: WorkspacePath,
    ) -> WorkspaceContext | None:
        """Capture and lock the path before deletion."""
        return await self.begin_workspace(path)

    async def after_workspace_delete(
        self,
        call_id: str,
        context: WorkspaceContext,
        result: PathOperationResult,
    ) -> str | None:
        """Notify deletion without analyzing the removed file."""
        return await self.finish_workspace(
            call_id,
            context,
            changes=[FileChange(result.path, 3)],
            paths=(),
        )

    async def before_workspace_mkdir(
        self,
        call_id: str,
        path: WorkspacePath,
    ) -> WorkspaceContext | None:
        """Capture and lock the directory path before creation."""
        return await self.begin_workspace(path)

    async def after_workspace_mkdir(
        self,
        call_id: str,
        context: WorkspaceContext,
        result: PathOperationResult,
    ) -> str | None:
        """Notify a newly created directory without collecting file analysis."""
        return await self.finish_workspace(
            call_id,
            context,
            changes=[FileChange(result.path, 1)] if result.action == "created" else (),
            paths=(),
        )

    async def error(self, call_id: str, context: WorkspaceContext) -> None:
        """Release the analysis lock after a failed workspace operation."""
        self.lock.release()

    async def finish_workspace(
        self,
        call_id: str,
        context: WorkspaceContext,
        *,
        changes: Sequence[FileChange] = (),
        paths: Sequence[WorkspacePath] | None = None,
    ) -> str | None:
        """Complete a provider call and release the lock acquired by before."""
        try:
            async with asyncio.timeout(self.ANALYSIS_TIMEOUT):
                if changes:
                    await self.synchronize_changes(changes)
                files = await self.analyze_files(context.paths if paths is None else paths)
                if not files:
                    return None
                return self.workspace.save_result_resource(
                    call_id,
                    "clangd",
                    "application/json",
                    ClangdResource(files=files).model_dump_json(),
                )
        except TimeoutError as error:
            await self.close_sessions()
            raise ClangdTimeoutError("Workspace analysis exceeded its timeout.") from error
        except BaseException:
            await self.close_sessions()
            raise
        finally:
            self.lock.release()

    async def synchronize_changes(self, changes: Sequence[FileChange]) -> None:
        """Refresh affected configurations and reopen retained documents after disk changes."""
        databases = {context.compilation_database for context in self.contexts.values()}
        if any(change.path in databases for change in changes):
            await self.cmake.publish_configurations(refresh=True)
            await self.update_configurations(await self.cmake.compilation_contexts())
        notifications: list[JsonValue] = [
            {
                "uri": self.workspace.resolve_workspace_path(change.path).as_uri(),
                "type": change.kind,
            }
            for change in changes
        ]
        for identifier, running in tuple(self.sessions.items()):
            try:
                await running.session.notify(
                    "workspace/didChangeWatchedFiles",
                    {"changes": notifications},
                )
                for document in tuple(running.session.documents.values()):
                    # Reopen after disk changes to rebuild dependent preambles
                    # and get versioned diagnostics even for unchanged text.
                    await running.session.forget(document.path)
                    try:
                        text = self.text(document.path)
                    except (WorkspaceError, ClangdError):
                        continue
                    else:
                        await running.session.synchronize(document.path, text)
            except BaseException:
                await self.close_session(identifier)
                raise

    async def analyze_files(self, paths: Sequence[WorkspacePath]) -> list[FileAnalysis]:
        """Collect diagnostic and highlighting snapshots for eligible managed source files."""
        files = []
        for path in dict.fromkeys(paths):
            if path.area == "root" or not self.contexts:
                continue
            native = self.workspace.resolve_workspace_path(path)
            if native.suffix.lower() not in self.SOURCE_EXTENSIONS or not native.is_file():
                continue
            text = self.text(path)

            async def run(session: ClangdSession) -> FileAnalysis:
                """Synchronize one file and capture versioned diagnostics and semantic spans."""
                version = await session.synchronize(path, text)
                diagnostics = await session.diagnostics(
                    path,
                    version,
                    timeout=self.ANALYSIS_TIMEOUT,
                )
                highlighting = await self.highlighting(
                    session,
                    path,
                    on_progress=None,
                    timeout=self.ANALYSIS_TIMEOUT,
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

            outcomes = await asyncio.gather(
                *(
                    self.run_context(context, run, None, self.ANALYSIS_TIMEOUT)
                    for context in self.contexts.values()
                ),
                return_exceptions=True,
            )
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            answers = cast(list[FileAnalysis], outcomes)
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
        return files

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        """Expose read-only analysis through one result-only MCP App."""
        read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False)

        def analysis_progress(ctx: Context) -> Progress:
            """Create a monotonically increasing progress callback for one analysis invocation."""
            report = progress(ctx, interval=self.progress_interval)
            updates = -1

            async def status(message: str) -> None:
                """Advance the invocation's progress counter and report the supplied message."""
                nonlocal updates
                updates += 1
                await report(updates, message=message)

            return status

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
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

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def clangd_diagnostics(
            path: WorkspacePath,
            ctx: Context,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[DiagnosticsResult]:
            """Get diagnostics for a managed file in selected configurations. Empty configurations selects all."""
            status = analysis_progress(ctx)
            await status("Starting clangd analysis")
            try:
                async def run(session: ClangdSession, version: int) -> DiagnosticsResult:
                    """Collect diagnostics for the synchronized document version in one
                    configuration.
                    """
                    return DiagnosticsResult(
                        configurations=[session.configuration.id],
                        path=path,
                        diagnostics=await session.diagnostics(
                            path,
                            version,
                            on_progress=status,
                            timeout=timeout,
                        ),
                    )

                result = await self.file_operation(
                    path,
                    configurations,
                    run,
                    on_progress=status,
                    timeout=timeout,
                )
            except (ClangdError, WorkspaceError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            await status("Completed clangd analysis")
            return result

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def clangd_hover(
            path: WorkspacePath,
            ctx: Context,
            position: Position,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[HoverResult]:
            """Describe the symbol at a one-based line and zero-based code-point position. Empty configurations selects all."""
            status = analysis_progress(ctx)
            await status("Starting clangd analysis")
            try:
                async def run(session: ClangdSession, version: int) -> HoverResult:
                    """Request and decode hover fragments for one configuration."""
                    self.require_capability(session, "hoverProvider")
                    raw = await session.request(
                        "textDocument/hover",
                        self.position_params(session, path, position),
                        on_progress=status,
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

                result = await self.file_operation(
                    path,
                    configurations,
                    run,
                    on_progress=status,
                    timeout=timeout,
                )
            except (ClangdError, WorkspaceError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            await status("Completed clangd analysis")
            return result

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def clangd_definition(
            path: WorkspacePath,
            ctx: Context,
            position: Position,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[DefinitionResult]:
            """Find definitions of the symbol at the supplied position.

            External locations use root/. Empty configurations selects all.
            """
            status = analysis_progress(ctx)
            await status("Starting clangd analysis")
            try:
                async def run(session: ClangdSession, version: int) -> DefinitionResult:
                    """Collect definition targets and source excerpts for one configuration."""
                    self.require_capability(session, "definitionProvider")
                    return DefinitionResult(
                        configurations=[session.configuration.id],
                        locations=await self.locations(
                            session,
                            "textDocument/definition",
                            self.position_params(session, path, position),
                            status,
                            timeout,
                        ),
                    )

                result = await self.file_operation(
                    path,
                    configurations,
                    run,
                    on_progress=status,
                    timeout=timeout,
                )
            except (ClangdError, WorkspaceError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            await status("Completed clangd analysis")
            return result

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def clangd_references(
            path: WorkspacePath,
            ctx: Context,
            position: Position,
            include_declaration: bool = True,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[ReferencesResult]:
            """Find references to the symbol at the supplied position. Empty configurations selects all."""
            status = analysis_progress(ctx)
            await status("Starting clangd analysis")
            try:
                async def run(session: ClangdSession, version: int) -> ReferencesResult:
                    """Collect reference targets and source excerpts for one configuration."""
                    self.require_capability(session, "referencesProvider")
                    params = self.position_params(session, path, position)
                    params["context"] = {"includeDeclaration": include_declaration}
                    return ReferencesResult(
                        configurations=[session.configuration.id],
                        locations=await self.locations(
                            session,
                            "textDocument/references",
                            params,
                            status,
                            timeout,
                        ),
                    )

                result = await self.file_operation(
                    path,
                    configurations,
                    run,
                    on_progress=status,
                    timeout=timeout,
                )
            except (ClangdError, WorkspaceError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            await status("Completed clangd analysis")
            return result

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def clangd_document_symbols(
            path: WorkspacePath,
            ctx: Context,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[DocumentSymbolsResult]:
            """List the symbols declared in a managed file. Empty configurations selects all."""
            status = analysis_progress(ctx)
            await status("Starting clangd analysis")
            try:
                async def run(session: ClangdSession, version: int) -> DocumentSymbolsResult:
                    """Decode hierarchical or flat document symbols for one configuration."""
                    self.require_capability(session, "documentSymbolProvider")
                    raw = await session.request(
                        "textDocument/documentSymbol",
                        {"textDocument": {"uri": session.uri(path)}},
                        on_progress=status,
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

                result = await self.file_operation(
                    path,
                    configurations,
                    run,
                    on_progress=status,
                    timeout=timeout,
                )
            except (ClangdError, WorkspaceError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            await status("Completed clangd analysis")
            return result

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def clangd_workspace_symbols(
            query: str,
            ctx: Context,
            configurations: list[str] = [],
            timeout: float = 30.0,
        ) -> list[WorkspaceSymbolsResult]:
            """Find workspace symbols by name query. Empty configurations selects all."""
            status = analysis_progress(ctx)
            await status("Starting clangd analysis")
            try:
                validate_timeout(timeout)

                async def run(session: ClangdSession) -> WorkspaceSymbolsResult:
                    """Decode matching workspace symbols and attach saved source excerpts."""
                    self.require_capability(session, "workspaceSymbolProvider")
                    raw = await session.request(
                        "workspace/symbol",
                        {"query": query},
                        on_progress=status,
                        timeout=timeout,
                    )
                    data = [
                        object_value(value)
                        for value in list_value([] if raw is None else raw)
                    ]
                    locations = self.navigation_locations(
                        [session.location(item.get("location")) for item in data],
                    )
                    symbols = []
                    for item, location in zip(data, locations, strict=True):
                        symbols.append(
                            WorkspaceSymbol(
                                name=string(item.get("name")),
                                kind=integer(item.get("kind")),
                                location=location,
                                container_name=item.get("containerName"),
                                tags=item.get("tags", []),
                            ),
                        )
                    return WorkspaceSymbolsResult(
                        configurations=[session.configuration.id],
                        symbols=symbols,
                    )

                async with self.operation(timeout):
                    contexts = await self.selection(configurations)
                    result = await self.collect(
                        contexts,
                        run,
                        on_progress=status,
                        timeout=timeout,
                    )
            except (ClangdError, WorkspaceError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            await status("Completed clangd analysis")
            return result

        apps.add_html_resource(self.WIDGET.uri, self.WIDGET.content)
