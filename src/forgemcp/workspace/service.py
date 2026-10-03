"""Workspace files, storage directories, and MCP registration."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import fnmatch
import json
import logging
import os
import stat
import tempfile
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Generator, Generic, Literal, TypeVar
from types import MappingProxyType
from urllib.parse import quote
from uuid import uuid4

import regex as regex_engine
from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context, Elicit, Resolve
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError, ToolError
from mcp.server.mcpserver.resources.templates import ResourceSecurity
from mcp.types import (
    Completion,
    CompletionArgument,
    CompletionContext,
    ResourceTemplateReference,
    ToolAnnotations,
)
from pydantic import BaseModel, Field

from forgemcp import markdown
from forgemcp.assets import IconFile, Widget
from forgemcp.completion import Complete
from forgemcp.progress import progress

from .errors import WorkspaceError
from .providers import ProviderCall, ResultResource, ResultResources
from .metadata import file_owner
from .path import WorkspacePath


class ReadConfirmation(BaseModel):
    allow: bool = Field(description="Allow reading this external file.")


async def confirm_read(path: WorkspacePath) -> ReadConfirmation | Elicit[ReadConfirmation]:
    if path.area != "root":
        return ReadConfirmation(allow=True)
    return Elicit(f"Allow reading external file {path.absolute}?", ReadConfirmation)


WorkspaceRoot = Literal["project", "storage"]


class TreeEntry(BaseModel):
    path: WorkspacePath
    kind: Literal["file", "directory", "symlink", "other"]
    children: list[TreeEntry] | None = None


class DirectoryTree(ResultResources):
    path: WorkspacePath
    entries: list[TreeEntry]


class FoundFile(BaseModel):
    path: WorkspacePath
    modified_at: datetime
    size_bytes: int


class FilePaths(ResultResources):
    paths: list[WorkspacePath]
    files: list[FoundFile] = Field(default_factory=list)


class FileContent(ResultResources):
    path: WorkspacePath
    text: str
    start_line: int


class FileInfo(ResultResources):
    path: WorkspacePath
    created_at: datetime | None
    modified_at: datetime
    size_bytes: int
    owner: str | None


class SearchMatch(BaseModel):
    path: WorkspacePath
    line: int
    text: str
    spans: list[tuple[int, int]] = Field(
        description="Match ranges as zero-based Unicode code point [start, end) pairs.",
    )


class SearchResult(ResultResources):
    matches: list[SearchMatch]
    skipped_files: list[WorkspacePath]


class FileWriteResult(ResultResources):
    path: WorkspacePath
    action: Literal["created", "overwritten"]
    lines_removed: int
    lines_added: int


class FileEditResult(ResultResources):
    path: WorkspacePath
    replacements: int


class PathOperationResult(ResultResources):
    path: WorkspacePath
    action: Literal["moved", "deleted", "created", "already_exists"]
    source: WorkspacePath | None = None


ContextT = TypeVar("ContextT")


class ResultProvider(Generic[ContextT]):
    """Typed hooks for Workspace tools; override only operations of interest."""

    async def before_workspace_list(
        self,
        call_id: str,
        path: WorkspacePath,
        depth: int | None,
        include_hidden: bool,
    ) -> ContextT | None:
        return None

    async def after_workspace_list(
        self,
        call_id: str,
        context: ContextT,
        result: DirectoryTree,
    ) -> str | None:
        return None

    async def before_workspace_find_files(
        self,
        call_id: str,
        pattern: str,
        path: WorkspacePath,
    ) -> ContextT | None:
        return None

    async def after_workspace_find_files(
        self,
        call_id: str,
        context: ContextT,
        result: FilePaths,
    ) -> str | None:
        return None

    async def before_workspace_file_info(
        self,
        call_id: str,
        path: WorkspacePath,
    ) -> ContextT | None:
        return None

    async def after_workspace_file_info(
        self,
        call_id: str,
        context: ContextT,
        result: FileInfo,
    ) -> str | None:
        return None

    async def before_workspace_read_file(
        self,
        call_id: str,
        path: WorkspacePath,
        confirm: ReadConfirmation,
        start_line: int,
        end_line: int | None,
    ) -> ContextT | None:
        return None

    async def after_workspace_read_file(
        self,
        call_id: str,
        context: ContextT,
        result: FileContent,
    ) -> str | None:
        return None

    async def before_workspace_search(
        self,
        call_id: str,
        query: str,
        path: WorkspacePath,
        regex: bool,
        extensions: list[str] | None,
        case_sensitive: bool,
    ) -> ContextT | None:
        return None

    async def after_workspace_search(
        self,
        call_id: str,
        context: ContextT,
        result: SearchResult,
    ) -> str | None:
        return None

    async def before_workspace_write_file(
        self,
        call_id: str,
        path: WorkspacePath,
        text: str,
    ) -> ContextT | None:
        return None

    async def after_workspace_write_file(
        self,
        call_id: str,
        context: ContextT,
        result: FileWriteResult,
    ) -> str | None:
        return None

    async def before_workspace_edit_file(
        self,
        call_id: str,
        path: WorkspacePath,
        old_text: str,
        new_text: str,
        replace_all: bool,
    ) -> ContextT | None:
        return None

    async def after_workspace_edit_file(
        self,
        call_id: str,
        context: ContextT,
        result: FileEditResult,
    ) -> str | None:
        return None

    async def before_workspace_move(
        self,
        call_id: str,
        source: WorkspacePath,
        destination: WorkspacePath,
    ) -> ContextT | None:
        return None

    async def after_workspace_move(
        self,
        call_id: str,
        context: ContextT,
        result: PathOperationResult,
    ) -> str | None:
        return None

    async def before_workspace_delete(
        self,
        call_id: str,
        path: WorkspacePath,
    ) -> ContextT | None:
        return None

    async def after_workspace_delete(
        self,
        call_id: str,
        context: ContextT,
        result: PathOperationResult,
    ) -> str | None:
        return None

    async def before_workspace_mkdir(
        self,
        call_id: str,
        path: WorkspacePath,
    ) -> ContextT | None:
        return None

    async def after_workspace_mkdir(
        self,
        call_id: str,
        context: ContextT,
        result: PathOperationResult,
    ) -> str | None:
        return None

    async def error(self, call_id: str, context: ContextT) -> None:
        pass


@contextmanager
def filesystem_errors(path: str | WorkspacePath) -> Generator[None]:
    """Translate expected failures without exposing absolute filesystem paths."""
    try:
        yield
    except UnicodeError as error:
        raise WorkspaceError(f"{path}: expected valid UTF-8 text.") from error
    except OSError as error:
        raise WorkspaceError(
            f"{path}: {error.strerror or type(error).__name__}."
        ) from error


def read_text(candidate: Path, path: WorkspacePath) -> str:
    """Read complete UTF-8 text without newline conversion for Workspace and diff."""
    with filesystem_errors(path):
        if not candidate.is_file():
            raise WorkspaceError(f"{path}: expected an existing regular file.")
        text = candidate.read_bytes().decode("utf-8")
        if "\0" in text:
            raise WorkspaceError(f"{path}: expected UTF-8 text, found a binary file.")
        return text


def replace_text(path: Path, text: str) -> None:
    """Write UTF-8 without newline translation, then replace the destination."""
    data = text.encode("utf-8")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=".forgemcp-",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        if path.exists():
            temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.chmod(temporary.stat().st_mode | stat.S_IWUSR)
            temporary.unlink(missing_ok=True)


class WorkspaceService:
    """Workspace: access and modify project files and separate service storage.

    Paths use / separators: project/... is relative to the project root,
    storage/... to the storage root. Neither permits . or .. segments.
    root/<absolute-path> identifies an external file; each explicit read requires
    approval, and writes and file mirror resources are unavailable there.

    Text is UTF-8; file resources mirror text or raw bytes. Searches skip dot
    directories and links. Read existing files before editing. Protected paths
    remain readable but cannot be modified.
    """

    TREE_WIDGET = Widget("assets/workspace-tree.html")
    FILE_WIDGET = Widget("assets/workspace-file.html")
    SEARCH_WIDGET = Widget("assets/workspace-search.html")
    RESULT_WIDGET = Widget("assets/workspace-result.html")
    ICON = IconFile("icons/workspace.svg")
    FILE_ICON = IconFile("icons/workspace-file.svg")
    SEARCH_ICON = IconFile("icons/workspace-search.svg")
    EDIT_ICON = IconFile("icons/workspace-edit.svg")
    FILE_URI = "forgemcp://workspace/file{/path*}"
    RAW_URI = "forgemcp://workspace/raw{/path*}"
    LIST_URI = "forgemcp://workspace/list{?path,depth,include_hidden}"
    FIND_URI = "forgemcp://workspace/find-files{?pattern,path}"
    INFO_URI = "forgemcp://workspace/file-info{?path}"
    SEARCH_URI = "forgemcp://workspace/search{?query,path,regex,extensions,case_sensitive}"
    RESULT_JSON_URI = "forgemcp://workspace/results/{result_id}/{name}.json"
    RESULT_MARKDOWN_URI = "forgemcp://workspace/results/{result_id}/{name}.md"
    PROVIDER_TOOLS = frozenset(
        (
            "workspace_list",
            "workspace_find_files",
            "workspace_file_info",
            "workspace_read_file",
            "workspace_search",
            "workspace_write_file",
            "workspace_edit_file",
            "workspace_move",
            "workspace_delete",
            "workspace_mkdir",
        ),
    )

    def __init__(
        self,
        workspace_root: Path,
        storage_root: Path | None = None,
        *,
        progress_interval: float = 1.0,
    ) -> None:
        self.progress_interval = progress_interval
        with filesystem_errors("project"):
            self.root = workspace_root.resolve()
            if not self.root.is_dir():
                raise WorkspaceError("Project root must be an existing directory.")
            self.storage_root = (
                storage_root or self.root.parent / f".{self.root.name}.forgemcp"
            ).resolve()
            if self.root.is_relative_to(self.storage_root):
                raise WorkspaceError(
                    "Storage must not be the project root or its ancestor."
                )
            if self.storage_root.exists() and not self.storage_root.is_dir():
                raise WorkspaceError("Storage root must be a directory.")
        self.protected_paths: set[Path] = set()
        self.result_providers: dict[
            str,
            tuple[ResultProvider, frozenset[str] | None],
        ] = {}
        self.result_resources: dict[str, dict[str, tuple[str, str]]] = {}
        self.operation_lock = asyncio.Lock()
        self.operation_owner: asyncio.Task[object] | None = None

    @asynccontextmanager
    async def serialized_operation(self) -> AsyncGenerator[None]:
        """Serialize Workspace entrypoints, allowing same-task resource delegation."""
        task = asyncio.current_task()
        if task is self.operation_owner:
            yield
            return
        async with self.operation_lock:
            self.operation_owner = task
            try:
                yield
            finally:
                self.operation_owner = None

    def read_result_resource(self, result_id: str, name: str) -> str:
        """Return an immutable resource without invoking its provider again."""
        try:
            return self.result_resources[result_id][name][1]
        except KeyError as error:
            raise WorkspaceError("Workspace result resource does not exist.") from error

    def register_provider(
        self,
        name: str,
        provider: ResultProvider,
        *,
        tools: Sequence[str] | None = None,
    ) -> None:
        """Register a before/after/error observer for selected Workspace tools."""
        if (
            not name
            or not name.isascii()
            or any(not (char.isalnum() or char in "_-") for char in name)
            or name in self.result_providers
        ):
            raise WorkspaceError("Provider needs a unique ASCII name.")
        if tools is not None and any(tool not in self.PROVIDER_TOOLS for tool in tools):
            raise WorkspaceError("Provider names an unknown Workspace tool.")
        self.result_providers[name] = (
            provider,
            frozenset(tools) if tools is not None else None,
        )

    def save_result_resource(
        self,
        call_id: str,
        provider: str,
        mime_type: Literal["application/json", "text/markdown"],
        text: str,
    ) -> str:
        """Store one immutable resource for a provider under the tool call ID."""
        if len(call_id) != 32 or any(char not in "0123456789abcdef" for char in call_id):
            raise WorkspaceError("Invalid Workspace call ID.")
        if provider not in self.result_providers:
            raise WorkspaceError("Unknown Workspace result provider.")
        if not isinstance(text, str):
            raise WorkspaceError("Result resource must contain text.")
        if mime_type == "application/json":
            try:
                json.loads(text)
            except ValueError as error:
                raise WorkspaceError("Result resource must contain valid JSON.") from error
            suffix = ".json"
        elif mime_type == "text/markdown":
            suffix = ".md"
        else:
            raise WorkspaceError("Result resource must be JSON or Markdown.")
        stored = self.result_resources.setdefault(call_id, {})
        if any(f"{provider}{extension}" in stored for extension in (".json", ".md")):
            raise WorkspaceError("Provider already saved a resource for this call.")
        name = provider + suffix
        stored[name] = (mime_type, text)
        return f"forgemcp://workspace/results/{call_id}/{name}"

    async def before_providers(
        self,
        tool_name: str,
        parameters: Mapping[str, object],
    ) -> ProviderCall:
        """Start every eligible provider with the same randomly generated ID."""
        if tool_name not in self.PROVIDER_TOOLS:
            raise WorkspaceError("Unknown Workspace provider operation.")
        call_id = uuid4().hex
        providers = {
            name: provider
            for name, (provider, tools) in self.result_providers.items()
            if tools is None or tool_name in tools
        }

        completed: dict[str, object] = {}

        async def run(name: str, provider: ResultProvider) -> tuple[object]:
            # Keep arbitrary context values distinct from gather's exceptions.
            method = getattr(provider, f"before_{tool_name}")
            context = await method(
                call_id,
                **deepcopy(dict(parameters)),
            )
            if context is not None:
                completed[name] = context
            return (context,)

        try:
            outcomes = await asyncio.gather(
                *(run(name, provider) for name, provider in providers.items()),
                return_exceptions=True,
            )
        except BaseException:
            # Completed before hooks may hold locks until after/error, even if
            # another provider is still running when this call is cancelled.
            await self.error_providers(
                ProviderCall(call_id, tool_name, MappingProxyType(completed)),
            )
            raise
        contexts = {}
        for name, outcome in zip(providers, outcomes):
            if isinstance(outcome, BaseException):
                logging.getLogger(__name__).warning("Workspace provider before failed: %s", name)
            elif outcome[0] is not None:
                contexts[name] = outcome[0]
        return ProviderCall(call_id, tool_name, MappingProxyType(contexts))

    async def after_providers(
        self,
        call: ProviderCall,
        result: BaseModel,
    ) -> ResultResources:
        """Wait for all successful before providers and attach their saved URIs."""
        names = list(call.contexts)
        finished: set[str] = set()

        async def run(name: str) -> str | None:
            try:
                provider = self.result_providers[name][0]
                method = getattr(provider, f"after_{call.tool_name}")
                return await method(
                    call.id,
                    call.contexts[name],
                    result.model_copy(deep=True),
                )
            finally:
                finished.add(name)

        try:
            outcomes = await asyncio.gather(
                *(run(name) for name in names),
                return_exceptions=True,
            )
        except BaseException:
            # Cancellation can prevent an after task from starting at all.
            await self.error_providers(
                ProviderCall(
                    call.id,
                    call.tool_name,
                    MappingProxyType({
                        name: call.contexts[name]
                        for name in names
                        if name not in finished
                    }),
                ),
            )
            raise
        stored = self.result_resources.get(call.id, {})
        links = {}
        for name, outcome in zip(names, outcomes):
            if isinstance(outcome, BaseException):
                logging.getLogger(__name__).warning("Workspace provider after failed: %s", name)
                continue
            expected = {
                f"forgemcp://workspace/results/{call.id}/{name}.json",
                f"forgemcp://workspace/results/{call.id}/{name}.md",
            }
            if outcome is None:
                continue
            if (
                not isinstance(outcome, str)
                or outcome not in expected
                or outcome.rsplit("/", 1)[-1] not in stored
            ):
                logging.getLogger(__name__).warning(
                    "Workspace provider returned no saved URI: %s",
                    name,
                )
                continue
            filename = outcome.rsplit("/", 1)[-1]
            links[name] = ResultResource(uri=outcome, mime_type=stored[filename][0])
        if links:
            kept = {link.uri.rsplit("/", 1)[-1] for link in links.values()}
            self.result_resources[call.id] = {
                name: value for name, value in stored.items() if name in kept
            }
        else:
            self.result_resources.pop(call.id, None)
        return ResultResources(resources=links)

    async def error_providers(self, call: ProviderCall) -> None:
        """Tell providers with a context that the Workspace tool failed."""

        async def run(name: str) -> None:
            provider = self.result_providers[name][0]
            await provider.error(call.id, call.contexts[name])

        try:
            outcomes = await asyncio.gather(
                *(run(name) for name in call.contexts),
                return_exceptions=True,
            )
            for name, outcome in zip(call.contexts, outcomes):
                if isinstance(outcome, BaseException):
                    logging.getLogger(__name__).warning("Workspace provider error failed: %s", name)
        finally:
            self.result_resources.pop(call.id, None)

    def root_path(self, root: WorkspaceRoot) -> Path:
        if root == "project":
            return self.root
        if root == "storage":
            return self.storage_root
        raise WorkspaceError("Root must be 'project' or 'storage'.")

    def resolve_path(self, path: str, *, root: WorkspaceRoot = "project") -> Path:
        """Return a checked absolute path, retaining its lexical link components."""
        if not path or "\0" in path:
            raise WorkspaceError("Path must be a nonempty relative path.")
        portable = PurePosixPath(path.replace("\\", "/"))
        windows = PureWindowsPath(path)
        if (
            portable.is_absolute()
            or windows.drive
            or windows.root
            or ".." in portable.parts
        ):
            raise WorkspaceError("Path must be relative and cannot contain '..'.")
        if os.name == "nt" and any(os.path.isreserved(part) for part in portable.parts):
            raise WorkspaceError("Path contains a reserved Windows name.")
        base = self.root_path(root)
        candidate = base.joinpath(*portable.parts)
        with filesystem_errors(path):
            if not candidate.resolve().is_relative_to(base):
                raise WorkspaceError("Path resolves outside the selected root.")
        return candidate

    def relative_path(self, path: Path, *, root: WorkspaceRoot = "project") -> str:
        try:
            relative = path.relative_to(self.root_path(root)).as_posix()
        except ValueError as error:
            raise WorkspaceError("Path is outside the selected root.") from error
        self.resolve_path(relative, root=root)
        return relative

    def is_link(self, path: Path) -> bool:
        return path.is_symlink() or path.is_junction()

    def check_path_links(self, path: Path, root: WorkspaceRoot) -> None:
        base = self.root_path(root)
        current = path
        while True:
            if self.is_link(current):
                raise WorkspaceError(
                    "This operation cannot traverse symbolic links or junctions."
                )
            if current == base:
                break
            current = current.parent

    def check_tree_links(self, path: Path) -> None:
        for directory, dirs, files in os.walk(
            path,
            followlinks=False,
            onerror=self.raise_walk_error,
        ):
            if any(self.is_link(Path(directory) / name) for name in [*dirs, *files]):
                raise WorkspaceError(
                    "This operation cannot move or remove a tree containing links."
                )

    def protect_path(self, path: WorkspacePath) -> None:
        candidate = self.resolve_workspace_path(path)
        self.protected_paths.update((candidate, candidate.resolve()))

    def writable_path(
        self,
        path: str,
        *,
        root: WorkspaceRoot,
        subtree: bool = False,
    ) -> Path:
        candidate = self.resolve_path(path, root=root)
        self.check_path_links(candidate, root)
        if candidate in (self.root, self.storage_root):
            raise WorkspaceError("Cannot change a workspace root.")
        for protected in self.protected_paths:
            if candidate.is_relative_to(protected) or (
                subtree and protected.is_relative_to(candidate)
            ):
                raise WorkspaceError(f"{path}: path is protected.")
        if subtree and self.storage_root.is_relative_to(candidate):
            raise WorkspaceError("Cannot move or remove a parent of the storage root.")
        return candidate

    def resolve_workspace_path(self, path: WorkspacePath) -> Path:
        return self.resolve_path(path.relative, root=path.area)

    def qualified_path(self, root: str, path: str) -> WorkspacePath:
        """Adapt a resource URI's root/path pair to the shared path type."""
        self.root_path(root)
        if not path:
            raise WorkspaceError("Path must not be empty.")
        try:
            return WorkspacePath(f"{root}/{'' if path == '.' else path}")
        except ValueError as error:
            raise WorkspaceError("Expected a canonical relative workspace path.") from error

    def workspace_path(self, path: Path) -> WorkspacePath:
        """Represent a native path without granting permission to access it."""
        candidate = path.absolute()
        for area in ("storage", "project"):
            base = self.root_path(area)
            if candidate.is_relative_to(base):
                relative = self.relative_path(candidate, root=area)
                return WorkspacePath(f"{area}/{'' if relative == '.' else relative}")
        return WorkspacePath("root/" + candidate.as_posix())

    def require_file(self, path: str, root: WorkspaceRoot) -> Path:
        candidate = self.resolve_path(path, root=root)
        if not candidate.is_file():
            raise WorkspaceError(f"{path}: expected an existing regular file.")
        return candidate

    def raise_walk_error(self, error: OSError) -> None:
        raise error

    async def iter_files(self, path: str, root: WorkspaceRoot) -> AsyncGenerator[Path]:
        start = self.resolve_path(path, root=root)
        self.check_path_links(start, root)
        relative = start.relative_to(self.root_path(root))
        directory_parts = relative.parts if start.is_dir() else relative.parts[:-1]
        if any(part.startswith(".") for part in directory_parts):
            return
        if start.is_file():
            yield start
            return
        if not start.is_dir():
            raise WorkspaceError(f"{path}: expected an existing file or directory.")
        for directory, dirs, files in os.walk(
            start,
            followlinks=False,
            onerror=self.raise_walk_error,
        ):
            await asyncio.sleep(0)
            parent = Path(directory)
            dirs[:] = sorted(
                name
                for name in dirs
                if not name.startswith(".") and not self.is_link(parent / name)
            )
            for name in sorted(files):
                child = parent / name
                if not self.is_link(child) and child.is_file():
                    yield child
                    await asyncio.sleep(0)

    def extension_matches(self, path: Path, extensions: Sequence[str] | None) -> bool:
        return extensions is None or path.suffix.removeprefix(".").lower() in {
            extension.removeprefix(".").lower() for extension in extensions
        }

    def file_uri(self, path: WorkspacePath) -> str:
        if path.area == "root":
            raise WorkspaceError("External files have no workspace mirror resources.")
        return f"forgemcp://workspace/file/{quote(str(path), safe='/')}"

    def render_markdown(
        self,
        result: DirectoryTree | FilePaths | FileInfo | SearchResult,
    ) -> str:
        """Render the same business results served by tools as Markdown resources."""
        nodes: list[markdown.Node] = []
        if isinstance(result, DirectoryTree):
            lines = [str(result.path)]

            def visit(entries: list[TreeEntry], prefix: str = "") -> None:
                for index, entry in enumerate(entries):
                    last = index == len(entries) - 1
                    suffix = (
                        "/"
                        if entry.kind == "directory"
                        else " @" if entry.kind == "symlink" else ""
                    )
                    lines.append(
                        f"{prefix}{'└── ' if last else '├── '}{PurePosixPath(entry.path.relative).name}{suffix}"
                    )
                    if entry.children is not None:
                        visit(entry.children, prefix + ("    " if last else "│   "))

            visit(result.entries)
            nodes.extend(
                [
                    markdown.Heading("Directory tree"),
                    markdown.CodeBlock("\n".join(lines)),
                ]
            )
        elif isinstance(result, FilePaths):
            nodes.extend(
                [
                    markdown.Heading("Files"),
                    markdown.UnorderedList(
                        markdown.Link(str(path), self.file_uri(path))
                        for path in result.paths
                    ),
                ]
            )
        elif isinstance(result, FileInfo):
            nodes.append(markdown.Heading("File information"))
            nodes.append(
                markdown.Table(
                    ["Field", "Value"],
                    [
                        [key, str(value) if value is not None else "Unavailable"]
                        for key, value in result.model_dump(mode="json").items()
                    ],
                )
            )
        else:
            nodes.append(markdown.Heading("Search results"))
            for match in result.matches:
                nodes.extend(
                    [
                        markdown.Paragraph(
                            f"{markdown.Link(str(match.path), self.file_uri(match.path))}, line {match.line}"
                        ),
                        markdown.CodeBlock(match.text),
                    ]
                )
            if not result.matches:
                nodes.append(markdown.Paragraph("No matches."))
            nodes.extend(
                [
                    markdown.Heading("Skipped files", level=2),
                    markdown.UnorderedList(str(path) for path in result.skipped_files),
                ]
            )
        return markdown.Document(nodes).render()

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        """Register file tools, mirrors, Markdown resources, and completions."""

        def serialized[**P, R](
            handler: Callable[P, Awaitable[R]],
        ) -> Callable[P, Awaitable[R]]:
            @wraps(handler)
            async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
                async with self.serialized_operation():
                    return await handler(*args, **kwargs)

            return wrapped

        read_only = ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            open_world_hint=False,
        )
        modifying = ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            open_world_hint=False,
        )

        @apps.tool(
            resource_uri=self.TREE_WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        @serialized
        async def workspace_list(
            ctx: Context,
            path: WorkspacePath = WorkspacePath("project/"),
            depth: int | None = 1,
            include_hidden: bool = False,
        ) -> DirectoryTree:
            """Show a directory tree; null depth expands every directory.

            File paths are errors. include_hidden controls dot directories;
            dot files remain visible and links are listed without traversal.
            """
            report_progress = progress(ctx, interval=self.progress_interval)
            call = await self.before_providers(
                "workspace_list",
                {
                    "path": path,
                    "depth": depth,
                    "include_hidden": include_hidden,
                },
            )
            try:
                visited = 0
                await report_progress(0, message="Starting directory scan")
                if depth is not None and depth < 1:
                    raise WorkspaceError(
                        "Depth must be positive or null for the complete tree."
                    )
                directory = self.resolve_workspace_path(path)
                self.check_path_links(directory, path.area)
                if not directory.is_dir():
                    raise WorkspaceError(f"{path}: expected an existing directory.")

                async def entries(
                    parent: Path,
                    remaining: int | None,
                ) -> list[TreeEntry]:
                    nonlocal visited
                    result: list[TreeEntry] = []
                    for child in sorted(parent.iterdir(), key=lambda item: item.name):
                        if (
                            not include_hidden
                            and child.name.startswith(".")
                            and child.is_dir()
                        ):
                            continue
                        children = None
                        if self.is_link(child):
                            kind = "symlink"
                        elif child.is_dir():
                            kind = "directory"
                            if remaining is None or remaining > 1:
                                children = await entries(
                                    child,
                                    None if remaining is None else remaining - 1,
                                )
                        elif child.is_file():
                            kind = "file"
                        else:
                            kind = "other"
                        entry = TreeEntry(
                            path=self.qualified_path(
                                path.area,
                                child.relative_to(self.root_path(path.area)).as_posix(),
                            ),
                            kind=kind,
                            children=children,
                        )
                        result.append(entry)
                        visited += 1
                        await report_progress(visited, message=f"process {entry.path}")
                        await asyncio.sleep(0)
                    return result

                with filesystem_errors(path):
                    result = DirectoryTree(
                        path=path,
                        entries=await entries(directory, depth),
                    )
            except BaseException as error:
                await self.error_providers(call)
                if isinstance(error, WorkspaceError):
                    raise ToolError(str(error)) from error
                raise
            result.resources = (await self.after_providers(call, result)).resources
            return result

        @apps.tool(
            resource_uri=self.TREE_WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        @serialized
        async def workspace_find_files(
            ctx: Context,
            pattern: str = "*",
            path: WorkspacePath = WorkspacePath("project/"),
        ) -> FilePaths:
            """Find file paths recursively by case-sensitive glob, skipping dot directories and links.

            Patterns without / match basenames; patterns with / match paths
            relative to the search directory.
            """
            report_progress = progress(ctx, interval=self.progress_interval)
            call = await self.before_providers(
                "workspace_find_files",
                {
                    "pattern": pattern,
                    "path": path,
                },
            )
            try:
                visited = 0
                await report_progress(0, message="Files visited")
                start = self.resolve_workspace_path(path)
                base = start if start.is_dir() else start.parent
                if not pattern:
                    raise WorkspaceError("File pattern must not be empty.")
                paths = []
                files = []
                with filesystem_errors(path):
                    async for candidate in self.iter_files(path.relative, path.area):
                        visited += 1
                        await report_progress(visited, message="Files visited")
                        target = candidate.relative_to(base).as_posix()
                        matches = (
                            PurePosixPath(target).full_match(
                                pattern,
                                case_sensitive=True,
                            )
                            if "/" in pattern
                            else fnmatch.fnmatchcase(candidate.name, pattern)
                        )
                        if matches:
                            found = self.qualified_path(
                                path.area,
                                self.relative_path(candidate, root=path.area),
                            )
                            paths.append(found)
                            metadata = candidate.stat()
                            files.append(
                                FoundFile(
                                    path=found,
                                    modified_at=datetime.fromtimestamp(metadata.st_mtime, UTC),
                                    size_bytes=metadata.st_size,
                                )
                            )
                result = FilePaths(
                    paths=sorted(paths, key=str),
                    files=sorted(files, key=lambda file: str(file.path)),
                )
            except BaseException as error:
                await self.error_providers(call)
                if isinstance(error, WorkspaceError):
                    raise ToolError(str(error)) from error
                raise
            result.resources = (await self.after_providers(call, result)).resources
            return result

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.FILE_ICON.icon],
            annotations=read_only,
        )
        @serialized
        async def workspace_file_info(
            path: WorkspacePath,
            ctx: Context,
        ) -> FileInfo:
            """Read creation/modification times, byte size, and owner of one file."""
            report_progress = progress(ctx, interval=self.progress_interval)
            call = await self.before_providers("workspace_file_info", {"path": path})
            try:
                await report_progress(0, total=1, message="Starting file info")
                with filesystem_errors(path):
                    candidate = self.require_file(path.relative, path.area)
                    metadata = candidate.stat()
                    birth = getattr(metadata, "st_birthtime", None)
                    result = FileInfo(
                        path=path,
                        created_at=(
                            datetime.fromtimestamp(birth, UTC) if birth is not None else None
                        ),
                        modified_at=datetime.fromtimestamp(metadata.st_mtime, UTC),
                        size_bytes=metadata.st_size,
                        owner=file_owner(candidate),
                    )
            except BaseException as error:
                await self.error_providers(call)
                if isinstance(error, WorkspaceError):
                    raise ToolError(str(error)) from error
                raise
            result.resources = (await self.after_providers(call, result)).resources
            await report_progress(1, total=1, message="Completed file info")
            return result

        @apps.tool(
            resource_uri=self.FILE_WIDGET.uri,
            icons=[self.FILE_ICON.icon],
            annotations=read_only,
        )
        @serialized
        async def workspace_read_file(
            path: WorkspacePath,
            ctx: Context,
            confirm: Annotated[ReadConfirmation, Resolve(confirm_read)],
            start_line: int = 1,
            end_line: int | None = None,
        ) -> FileContent:
            """Read UTF-8 text, optionally selecting an inclusive range of one-based lines."""
            report_progress = progress(ctx, interval=self.progress_interval)
            call = await self.before_providers(
                "workspace_read_file",
                {
                    "path": path,
                    "confirm": confirm,
                    "start_line": start_line,
                    "end_line": end_line,
                },
            )
            try:
                if start_line < 1 or (end_line is not None and end_line < start_line):
                    raise WorkspaceError(
                        "Line range must start at 1 or later and end at or after its start."
                    )
                with filesystem_errors(path):
                    if not confirm.allow:
                        raise WorkspaceError("External file reading was not permitted.")
                    candidate = (
                        path.absolute if path.area == "root"
                        else self.require_file(path.relative, path.area)
                    )
                    if not candidate.is_file():
                        raise WorkspaceError(f"{path}: expected an existing regular file.")
                    text = bytearray()
                    await report_progress(0, message="Bytes read")
                    with candidate.open("rb") as stream:
                        while chunk := stream.read(64 * 1024):
                            text.extend(chunk)
                            await report_progress(len(text), message="Bytes read")
                            await asyncio.sleep(0)
                    decoded = text.decode("utf-8")
                    if "\0" in decoded:
                        raise WorkspaceError(
                            f"{path}: expected UTF-8 text, found a binary file."
                        )
                    result = FileContent(
                        path=path,
                        text="".join(
                            decoded.splitlines(keepends=True)[start_line - 1 : end_line]
                        ),
                        start_line=start_line,
                    )
            except BaseException as error:
                await self.error_providers(call)
                if isinstance(error, WorkspaceError):
                    raise ToolError(str(error)) from error
                raise
            result.resources = (await self.after_providers(call, result)).resources
            return result

        @apps.tool(
            resource_uri=self.SEARCH_WIDGET.uri,
            icons=[self.SEARCH_ICON.icon],
            annotations=read_only,
        )
        @serialized
        async def workspace_search(
            query: str,
            ctx: Context,
            path: WorkspacePath = WorkspacePath("project/"),
            regex: bool = False,
            extensions: list[str] | None = None,
            case_sensitive: bool = True,
        ) -> SearchResult:
            """Search lines by literal text or regex; report skipped binary/non-UTF-8 files.

            extensions accepts suffixes with or without a leading dot;
            null selects all files, while an empty list selects none.
            """
            report_progress = progress(ctx, interval=self.progress_interval)
            call = await self.before_providers(
                "workspace_search",
                {
                    "query": query,
                    "path": path,
                    "regex": regex,
                    "extensions": extensions,
                    "case_sensitive": case_sensitive,
                },
            )
            try:
                visited = 0
                await report_progress(0, message="Files visited")
                if not query:
                    raise WorkspaceError("Search query must not be empty.")
                try:
                    expression = regex_engine.compile(
                        query if regex else regex_engine.escape(query),
                        flags=regex_engine.VERSION1
                        | (0 if case_sensitive else regex_engine.IGNORECASE),
                    )
                except regex_engine.error as error:
                    raise WorkspaceError(
                        f"Invalid regular expression: {error}"
                    ) from error
                matches, skipped = [], []
                with filesystem_errors(path):
                    async for candidate in self.iter_files(path.relative, path.area):
                        visited += 1
                        current = self.qualified_path(
                            path.area,
                            self.relative_path(candidate, root=path.area),
                        )
                        await report_progress(visited, message=f"Searching {current}")
                        if not self.extension_matches(candidate, extensions):
                            continue
                        relative = self.qualified_path(
                            path.area,
                            self.relative_path(candidate, root=path.area),
                        )
                        data = candidate.read_bytes()
                        try:
                            text = data.decode("utf-8")
                        except UnicodeDecodeError:
                            skipped.append(relative)
                            continue
                        if "\0" in text:
                            skipped.append(relative)
                            continue
                        for line, value in enumerate(text.splitlines(), 1):
                            if line % 256 == 0:
                                await asyncio.sleep(0)
                            spans = [
                                match.span() for match in expression.finditer(value)
                            ]
                            if spans:
                                matches.append(
                                    SearchMatch(
                                        path=relative,
                                        line=line,
                                        text=value,
                                        spans=spans,
                                    )
                                )
                result = SearchResult(
                    matches=sorted(matches, key=lambda item: (str(item.path), item.line)),
                    skipped_files=sorted(skipped, key=str),
                )
            except BaseException as error:
                await self.error_providers(call)
                if isinstance(error, WorkspaceError):
                    raise ToolError(str(error)) from error
                raise
            result.resources = (await self.after_providers(call, result)).resources
            return result

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        @serialized
        async def workspace_write_file(
            path: WorkspacePath,
            text: str,
            ctx: Context,
        ) -> FileWriteResult:
            """Create or fully overwrite a UTF-8 file; its parent directory must exist.

            Return removed/added line counts and linked change resources.
            """
            report_progress = progress(ctx, interval=self.progress_interval)
            call = await self.before_providers(
                "workspace_write_file",
                {
                    "path": path,
                    "text": text,
                },
            )
            try:
                await report_progress(0, total=1, message="Starting write file")
                with filesystem_errors(path):
                    candidate = self.writable_path(path.relative, root=path.area)
                    existed = candidate.exists()
                    previous = read_text(candidate, path) if existed else ""
                    replace_text(candidate, text)
                    result = FileWriteResult(
                        path=path,
                        action="overwritten" if existed else "created",
                        lines_removed=len(previous.splitlines()),
                        lines_added=len(text.splitlines()),
                    )
            except BaseException as error:
                await self.error_providers(call)
                if isinstance(error, WorkspaceError):
                    raise ToolError(str(error)) from error
                raise
            result.resources = (await self.after_providers(call, result)).resources
            await report_progress(1, total=1, message="Completed write file")
            return result

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        @serialized
        async def workspace_edit_file(
            path: WorkspacePath,
            old_text: str,
            new_text: str,
            ctx: Context,
            replace_all: bool = False,
        ) -> FileEditResult:
            """Replace one exact text occurrence, or all with replace_all=true.

            old_text must be nonempty. Missing or ambiguous matches leave the
            file unchanged. Text and line endings outside replacements are preserved.
            """
            report_progress = progress(ctx, interval=self.progress_interval)
            call = await self.before_providers(
                "workspace_edit_file",
                {
                    "path": path,
                    "old_text": old_text,
                    "new_text": new_text,
                    "replace_all": replace_all,
                },
            )
            try:
                await report_progress(0, total=1, message="Starting edit file")
                if not old_text:
                    raise WorkspaceError("old_text must not be empty.")
                with filesystem_errors(path):
                    candidate = self.writable_path(path.relative, root=path.area)
                    text = read_text(candidate, path)
                    count = text.count(old_text)
                    if count == 0:
                        raise WorkspaceError(
                            "Exact text was not found; the file was not changed."
                        )
                    if count > 1 and not replace_all:
                        raise WorkspaceError(
                            f"Found {count} occurrences; use replace_all or a more specific old_text."
                        )
                    replace_text(candidate, text.replace(old_text, new_text))
                    result = FileEditResult(
                        path=path,
                        replacements=count,
                    )
            except BaseException as error:
                await self.error_providers(call)
                if isinstance(error, WorkspaceError):
                    raise ToolError(str(error)) from error
                raise
            result.resources = (await self.after_providers(call, result)).resources
            await report_progress(1, total=1, message="Completed edit file")
            return result

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        @serialized
        async def workspace_move(
            source: WorkspacePath,
            destination: WorkspacePath,
            ctx: Context,
        ) -> PathOperationResult:
            """Move a file or directory within project/ or within storage/.

            The destination must not exist, and its parent directory must exist.
            """
            report_progress = progress(ctx, interval=self.progress_interval)
            call = await self.before_providers(
                "workspace_move",
                {
                    "source": source,
                    "destination": destination,
                },
            )
            try:
                await report_progress(0, total=1, message="Starting move")
                if source.area != destination.area:
                    raise WorkspaceError("Move source and destination must use the same root.")
                with filesystem_errors(source):
                    origin = self.writable_path(
                        source.relative,
                        root=source.area,
                        subtree=True,
                    )
                    target = self.writable_path(
                        destination.relative,
                        root=destination.area,
                        subtree=True,
                    )
                    if not origin.exists():
                        raise WorkspaceError(f"{source}: path does not exist.")
                    if target.exists() or target.is_symlink():
                        raise WorkspaceError(f"{destination}: destination already exists.")
                    if origin.is_dir():
                        if target.is_relative_to(origin):
                            raise WorkspaceError("Cannot move a directory into itself.")
                        self.check_tree_links(origin)
                    origin.rename(target)
                    result = PathOperationResult(
                        path=destination,
                        action="moved",
                        source=source,
                    )
            except BaseException as error:
                await self.error_providers(call)
                if isinstance(error, WorkspaceError):
                    raise ToolError(str(error)) from error
                raise
            result.resources = (await self.after_providers(call, result)).resources
            await report_progress(1, total=1, message="Completed move")
            return result

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        @serialized
        async def workspace_delete(
            path: WorkspacePath,
            ctx: Context,
        ) -> PathOperationResult:
            """Delete a file or empty directory; protected paths and roots cannot be deleted."""
            report_progress = progress(ctx, interval=self.progress_interval)
            call = await self.before_providers("workspace_delete", {"path": path})
            try:
                await report_progress(0, total=1, message="Starting delete")
                with filesystem_errors(path):
                    candidate = self.writable_path(
                        path.relative,
                        root=path.area,
                        subtree=True,
                    )
                    if candidate.is_dir():
                        candidate.rmdir()
                    else:
                        self.require_file(path.relative, path.area).unlink()
                    result = PathOperationResult(
                        path=path,
                        action="deleted",
                    )
            except BaseException as error:
                await self.error_providers(call)
                if isinstance(error, WorkspaceError):
                    raise ToolError(str(error)) from error
                raise
            result.resources = (await self.after_providers(call, result)).resources
            await report_progress(1, total=1, message="Completed delete")
            return result

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        @serialized
        async def workspace_mkdir(
            path: WorkspacePath,
            ctx: Context,
        ) -> PathOperationResult:
            """Create a directory and missing parents, or report that it already exists."""
            report_progress = progress(ctx, interval=self.progress_interval)
            call = await self.before_providers("workspace_mkdir", {"path": path})
            try:
                await report_progress(0, total=1, message="Starting mkdir")
                with filesystem_errors(path):
                    candidate = self.writable_path(path.relative, root=path.area)
                    existed = candidate.is_dir()
                    candidate.mkdir(parents=True, exist_ok=True)
                    result = PathOperationResult(
                        path=path,
                        action="already_exists" if existed else "created",
                    )
            except BaseException as error:
                await self.error_providers(call)
                if isinstance(error, WorkspaceError):
                    raise ToolError(str(error)) from error
                raise
            result.resources = (await self.after_providers(call, result)).resources
            await report_progress(1, total=1, message="Completed mkdir")
            return result

        for widget in (
            self.TREE_WIDGET,
            self.FILE_WIDGET,
            self.SEARCH_WIDGET,
            self.RESULT_WIDGET,
        ):
            apps.add_html_resource(widget.uri, widget.content)

        @mcp.resource(
            self.RESULT_JSON_URI,
            mime_type="application/json",
            icons=[self.FILE_ICON.icon],
        )
        @serialized
        async def workspace_result_json(result_id: str, name: str) -> str:
            """Read immutable provider JSON from one tool invocation."""
            try:
                return self.read_result_resource(result_id, name + ".json")
            except WorkspaceError as error:
                raise ResourceNotFoundError(str(error)) from error

        @mcp.resource(
            self.RESULT_MARKDOWN_URI,
            mime_type="text/markdown",
            icons=[self.FILE_ICON.icon],
        )
        @serialized
        async def workspace_result_markdown(result_id: str, name: str) -> str:
            """Read a provider's immutable Markdown, independently of syntax token data."""
            try:
                return self.read_result_resource(result_id, name + ".md")
            except WorkspaceError as error:
                raise ResourceNotFoundError(str(error)) from error

        def resource_path(value: str) -> WorkspacePath:
            try:
                # Directory completions end in '/', while WorkspacePath is canonical.
                area, separator, relative = value.partition("/")
                if area == "root":
                    raise ResourceNotFoundError("External files have no workspace mirror resources.")
                if not separator:
                    raise WorkspaceError("Path must start with project/ or storage/.")
                return self.qualified_path(area, relative.rstrip("/") or ".")
            except WorkspaceError as error:
                raise ResourceError(str(error)) from error

        @mcp.resource(
            self.FILE_URI,
            mime_type="text/plain",
            icons=[self.FILE_ICON.icon],
        )
        @serialized
        async def workspace_file_resource(
            path: list[str] | None = None,
        ) -> str:
            """Read the complete UTF-8 file without formatting or metadata."""
            selected = resource_path("/".join(path or []))
            try:
                return read_text(self.resolve_workspace_path(selected), selected)
            except WorkspaceError as error:
                raise ResourceError(str(error)) from error

        @mcp.resource(
            self.RAW_URI,
            mime_type="application/octet-stream",
            icons=[self.FILE_ICON.icon],
        )
        @serialized
        async def workspace_raw_resource(
            path: list[str] | None = None,
        ) -> bytes:
            """Read the exact bytes of any file."""
            selected = resource_path("/".join(path or []))
            try:
                with filesystem_errors(selected):
                    return self.require_file(selected.relative, selected.area).read_bytes()
            except WorkspaceError as error:
                raise ResourceError(str(error)) from error

        @mcp.resource(self.LIST_URI, mime_type="text/markdown", icons=[self.ICON.icon])
        @serialized
        async def workspace_list_resource(
            ctx: Context,
            path: str = "project/",
            depth: int | Literal["all"] = 1,
            include_hidden: bool = False,
        ) -> str:
            """Read a directory tree as Markdown; depth=all expands the whole tree."""
            try:
                result = await workspace_list(
                    ctx=ctx,
                    path=resource_path(path),
                    depth=None if depth == "all" else depth,
                    include_hidden=include_hidden,
                )
                return self.render_markdown(result)
            except (WorkspaceError, ToolError) as error:
                raise ResourceError(str(error)) from error

        @mcp.resource(
            self.FIND_URI,
            mime_type="text/markdown",
            icons=[self.ICON.icon],
            security=ResourceSecurity(exempt_params={"pattern"}),
        )
        @serialized
        async def workspace_find_files_resource(
            ctx: Context,
            pattern: str = "*",
            path: str = "project/",
        ) -> str:
            """Read matching file links as Markdown."""
            try:
                result = await workspace_find_files(
                    ctx=ctx,
                    pattern=pattern,
                    path=resource_path(path),
                )
                return self.render_markdown(result)
            except (WorkspaceError, ToolError) as error:
                raise ResourceError(str(error)) from error

        @mcp.resource(
            self.INFO_URI,
            mime_type="text/markdown",
            icons=[self.FILE_ICON.icon],
        )
        @serialized
        async def workspace_file_info_resource(
            ctx: Context,
            path: str = "",
        ) -> str:
            """Read one file's metadata as a Markdown table; path is required."""
            selected = resource_path(path)
            try:
                result = await workspace_file_info(
                    path=selected,
                    ctx=ctx,
                )
                return self.render_markdown(result)
            except (WorkspaceError, ToolError) as error:
                raise ResourceError(str(error)) from error

        @mcp.resource(
            self.SEARCH_URI,
            mime_type="text/markdown",
            icons=[self.SEARCH_ICON.icon],
            security=ResourceSecurity(exempt_params={"query"}),
        )
        @serialized
        async def workspace_search_resource(
            ctx: Context,
            query: str = "",
            path: str = "project/",
            regex: bool = False,
            extensions: str | None = None,
            case_sensitive: bool = True,
        ) -> str:
            """Read text/regex matches and skipped files as Markdown; query is required."""
            try:
                result = await workspace_search(
                    ctx=ctx,
                    query=query,
                    path=resource_path(path),
                    regex=regex,
                    extensions=None if extensions is None else extensions.split(","),
                    case_sensitive=case_sensitive,
                )
                return self.render_markdown(result)
            except (WorkspaceError, ToolError) as error:
                raise ResourceError(str(error)) from error

        async def workspace_completion(
            ref: ResourceTemplateReference,
            argument: CompletionArgument,
            context: CompletionContext | None,
        ) -> Completion | None:
            if isinstance(ref, ResourceTemplateReference) and ref.uri in (
                self.RESULT_JSON_URI,
                self.RESULT_MARKDOWN_URI,
            ):
                if argument.name == "result_id":
                    values = list(self.result_resources)
                elif argument.name == "name":
                    result_id = (context.arguments or {}).get("result_id", "") if context else ""
                    suffix = ".json" if ref.uri == self.RESULT_JSON_URI else ".md"
                    values = [
                        name.removesuffix(suffix)
                        for name in self.result_resources.get(result_id, {})
                        if name.endswith(suffix)
                    ]
                else:
                    values = []
                matches = [value for value in values if value.startswith(argument.value)]
                return Completion(
                    values=matches[:100],
                    total=len(matches),
                    has_more=len(matches) > 100,
                )
            uris = (
                self.FILE_URI,
                self.RAW_URI,
                self.LIST_URI,
                self.FIND_URI,
                self.INFO_URI,
                self.SEARCH_URI,
            )
            if not isinstance(ref, ResourceTemplateReference) or ref.uri not in uris:
                return None
            values: list[str] = []
            if argument.name in ("regex", "case_sensitive", "include_hidden"):
                values = ["false", "true"]
            elif argument.name == "depth":
                values = ["1", "2", "3", "all"]
            elif argument.name == "extensions" and ref.uri == self.SEARCH_URI:
                selected_path = (
                    (context.arguments or {}).get("path", "project/")
                    if context
                    else "project/"
                )
                try:
                    selected = resource_path(selected_path)
                    extensions = sorted(
                        {
                            candidate.suffix.removeprefix(".")
                            async for candidate in self.iter_files(selected.relative, selected.area)
                        }
                    )
                    prefix, separator, tail = argument.value.rpartition(",")
                    values = [
                        prefix + separator + value
                        for value in extensions
                        if value.startswith(tail)
                    ]
                except (WorkspaceError, ResourceError):
                    values = []
            elif argument.name == "path":
                if "/" not in argument.value:
                    values = ["project/", "storage/"]
                else:
                    try:
                        parent = argument.value.rpartition("/")[0]
                        if parent in ("project", "storage"):
                            parent += "/"
                        selected = resource_path(parent)
                        directory = self.resolve_workspace_path(selected)
                        self.check_path_links(directory, selected.area)
                        with filesystem_errors(parent):
                            for child in sorted(directory.iterdir()):
                                await asyncio.sleep(0)
                                if self.is_link(child):
                                    continue
                                is_directory = child.is_dir()
                                if (
                                    ref.uri in (self.LIST_URI, self.FIND_URI, self.SEARCH_URI)
                                    and not is_directory
                                ):
                                    continue
                                if is_directory or child.is_file():
                                    relative = self.relative_path(child, root=selected.area)
                                    values.append(
                                        str(self.qualified_path(selected.area, relative))
                                        + ("/" if is_directory else "")
                                    )
                    except (WorkspaceError, ResourceError):
                        values = []
            matches = [value for value in values if value.startswith(argument.value)]
            return Completion(
                values=matches[:100],
                total=len(matches),
                has_more=len(matches) > 100,
            )

        complete.add_completion(serialized(workspace_completion))
