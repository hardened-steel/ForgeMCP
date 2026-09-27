"""Workspace files, storage directories, and MCP registration."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Generator, Literal
from types import MappingProxyType
from urllib.parse import quote
from uuid import uuid4

import regex as regex_engine
from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
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
from .extensions import (
    ExtensionContext,
    ExtensionOutput,
    ExtensionProvider,
    ResultExtension,
    ResultExtensions,
    ResultResource,
    ResultResources,
    TextChange,
)
from .metadata import file_owner
from .path import WorkspacePath

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


class StorageDirectory:
    """A directory in the workspace's persistent or temporary storage."""

    def __init__(self, workspace: WorkspaceService, path: Path) -> None:
        self.workspace = workspace
        self.path = workspace.resolve_path(
            workspace.relative_path(path, root="storage"),
            root="storage",
        )

    def subdirectory(self, key: str) -> StorageDirectory:
        self.workspace.validate_directory_key(key)
        relative = self.workspace.relative_path(self.path / key, root="storage")
        self.workspace.mkdir(self.workspace.qualified_path("storage", relative))
        return StorageDirectory(self.workspace, self.path / key)

    def remove(self) -> None:
        relative = self.workspace.relative_path(self.path, root="storage")
        self.workspace.remove_directory(self.workspace.qualified_path("storage", relative))


class WorkspaceService:
    """Browse project/storage trees, search files, and read or change UTF-8 text.

    Paths use project/... or storage/... strings. Searches skip dot directories
    and links. Read files before editing; edits replace one exact text occurrence
    unless replace_all is requested. File resources mirror text or raw bytes.
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
        self.extension_providers: dict[
            str,
            tuple[str, int, ExtensionProvider, frozenset[str] | None],
        ] = {}
        self.result_resources: dict[str, dict[str, tuple[str, str]]] = {}

    def register_extension(
        self,
        name: str,
        provider: ExtensionProvider,
        *,
        kind: str,
        version: int = 1,
        tools: Sequence[str] | None = None,
    ) -> None:
        """Register one uniquely named provider; workspace does not interpret its data."""
        if not name or not kind or version < 1 or name in self.extension_providers:
            raise WorkspaceError("Extension needs a unique name, kind and positive version.")
        self.extension_providers[name] = (
            kind,
            version,
            provider,
            frozenset(tools) if tools is not None else None,
        )

    def extensions_for(self, tool_name: str) -> dict[str, tuple[str, int, ExtensionProvider]]:
        return {
            name: (kind, version, provider)
            for name, (kind, version, provider, tools) in self.extension_providers.items()
            if tools is None or tool_name in tools
        }

    async def create_result_extensions(
        self,
        context: ExtensionContext,
    ) -> ResultResources:
        """Freeze provider output once; subsequent resource reads only retrieve text."""
        providers = self.extensions_for(context.tool_name)
        if not providers:
            return ResultResources()
        result_id = uuid4().hex
        base_uri = f"forgemcp://workspace/results/{result_id}/"
        stored = {}
        extensions = []
        links = []
        for name, (kind, version, provider) in providers.items():
            entry = ResultExtension(name=name, kind=kind, version=version)
            try:
                # Every provider sees an independent result object and immutable text mapping.
                output = await provider(
                    ExtensionContext(
                        tool_name=context.tool_name,
                        result=context.result.model_copy(deep=True),
                        paths=context.paths,
                        texts=MappingProxyType(dict(context.texts)),
                        change=context.change,
                    )
                )
                if output is None:
                    continue
                if not isinstance(output, ExtensionOutput):
                    raise WorkspaceError("Provider must return ExtensionOutput or None.")
                pending = {}
                for resource in output.resources:
                    expected_mime = {
                        ".json": "application/json",
                        ".md": "text/markdown",
                    }.get(Path(resource.name).suffix)
                    if (
                        not resource.name
                        or not resource.name.isascii()
                        or any(
                            not (char.isalnum() or char in "._-")
                            for char in resource.name
                        )
                        or ".." in resource.name
                        or resource.name == "extensions.json"
                        or resource.name in stored
                        or resource.name in pending
                        or expected_mime is None
                        or resource.mime_type != expected_mime
                        or not isinstance(resource.text, str)
                    ):
                        raise WorkspaceError(
                            "Extension resources need unique .json/.md filenames and matching MIME types."
                        )
                    if resource.mime_type == "application/json":
                        json.loads(resource.text)
                    pending[resource.name] = (resource.mime_type, resource.text)
                entry = ResultExtension(
                    name=name,
                    kind=kind,
                    version=version,
                    data=output.data,
                )
                # Detach mutable JSON supplied by the provider before another await.
                entry = ResultExtension.model_validate_json(entry.model_dump_json())
                stored.update(pending)
                links.extend(
                    ResultResource(uri=base_uri + filename, mime_type=mime_type)
                    for filename, (mime_type, _) in pending.items()
                )
            except Exception:
                logging.getLogger(__name__).warning("Workspace extension failed: %s", name)
                entry = ResultExtension(
                    name=name,
                    kind=kind,
                    version=version,
                    error="Extension provider failed.",
                )
            extensions.append(entry)
        if not extensions:
            return ResultResources()
        manifest = ResultExtensions(extensions=extensions, resources=links)
        stored["extensions.json"] = ("application/json", manifest.model_dump_json())
        self.result_resources[result_id] = stored
        return ResultResources(
            extensions_uri=base_uri + "extensions.json",
            resources=links,
        )

    def read_result_resource(self, result_id: str, name: str) -> str:
        """Return an immutable resource without invoking its provider again."""
        try:
            return self.result_resources[result_id][name][1]
        except KeyError as error:
            raise WorkspaceError("Workspace result resource does not exist.") from error

    async def enrich_result[T: ResultResources](
        self,
        tool_name: str,
        result: T,
        paths: Sequence[WorkspacePath],
        texts: Mapping[WorkspacePath, str] | None = None,
        change: TextChange | None = None,
    ) -> T:
        references = await self.create_result_extensions(
            ExtensionContext(
                tool_name=tool_name,
                result=result,
                paths=tuple(dict.fromkeys(paths)),
                texts=texts or {},
                change=change,
            )
        )
        result.extensions_uri = references.extensions_uri
        result.resources = references.resources
        return result

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
        """Represent an absolute local path using one of the configured roots."""
        candidate = path.absolute()
        for area in ("storage", "project"):
            base = self.root_path(area)
            if candidate.is_relative_to(base):
                relative = self.relative_path(candidate, root=area)
                return WorkspacePath(f"{area}/{'' if relative == '.' else relative}")
        raise WorkspaceError("Path is outside the project and storage roots.")

    def require_file(self, path: str, root: WorkspaceRoot) -> Path:
        candidate = self.resolve_path(path, root=root)
        if not candidate.is_file():
            raise WorkspaceError(f"{path}: expected an existing regular file.")
        return candidate

    def raise_walk_error(self, error: OSError) -> None:
        raise error

    async def iter_files(self, path: str, root: WorkspaceRoot) -> AsyncIterator[Path]:
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

    def read_bytes(self, path: WorkspacePath) -> bytes:
        with filesystem_errors(path):
            return self.require_file(path.relative, path.area).read_bytes()

    def read_file(
        self,
        path: WorkspacePath,
        *,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> FileContent:
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise WorkspaceError(
                "Line range must start at 1 or later and end at or after its start."
            )
        with filesystem_errors(path):
            text = self.read_bytes(path).decode("utf-8")
            if "\0" in text:
                raise WorkspaceError(f"{path}: binary file; use the raw resource.")
        return FileContent(
            path=path,
            text="".join(text.splitlines(keepends=True)[start_line - 1 : end_line]),
            start_line=start_line,
        )

    def file_info(self, path: WorkspacePath) -> FileInfo:
        with filesystem_errors(path):
            candidate = self.require_file(path.relative, path.area)
            metadata = candidate.stat()
            birth = getattr(metadata, "st_birthtime", None)
            return FileInfo(
                path=path,
                created_at=(
                    datetime.fromtimestamp(birth, UTC) if birth is not None else None
                ),
                modified_at=datetime.fromtimestamp(metadata.st_mtime, UTC),
                size_bytes=metadata.st_size,
                owner=file_owner(candidate),
            )

    def replace_text(self, path: Path, text: str) -> None:
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

    def write_file(
        self,
        path: WorkspacePath,
        text: str,
        *,
        on_change: Callable[[TextChange], None] | None = None,
    ) -> FileWriteResult:
        with filesystem_errors(path):
            candidate = self.writable_path(path.relative, root=path.area)
            existed = candidate.exists()
            previous = self.read_file(path).text if existed else ""
            self.replace_text(candidate, text)
            if on_change is not None:
                on_change(TextChange(path=path, before=previous, after=text))
            return FileWriteResult(
                path=path,
                action="overwritten" if existed else "created",
                lines_removed=len(previous.splitlines()),
                lines_added=len(text.splitlines()),
            )

    def edit_file(
        self,
        path: WorkspacePath,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
        on_change: Callable[[TextChange], None] | None = None,
    ) -> FileEditResult:
        if not old_text:
            raise WorkspaceError("old_text must not be empty.")
        with filesystem_errors(path):
            candidate = self.writable_path(path.relative, root=path.area)
            text = self.read_file(path).text
            count = text.count(old_text)
            if count == 0:
                raise WorkspaceError(
                    "Exact text was not found; the file was not changed."
                )
            if count > 1 and not replace_all:
                raise WorkspaceError(
                    f"Found {count} occurrences; use replace_all or a more specific old_text."
                )
            updated = text.replace(old_text, new_text)
            self.replace_text(candidate, updated)
            if on_change is not None:
                on_change(TextChange(path=path, before=text, after=updated))
            return FileEditResult(
                path=path,
                replacements=count,
            )

    def move(
        self,
        source: WorkspacePath,
        destination: WorkspacePath,
    ) -> PathOperationResult:
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
            return PathOperationResult(
                path=destination,
                action="moved",
                source=source,
            )

    def delete(
        self,
        path: WorkspacePath,
    ) -> PathOperationResult:
        with filesystem_errors(path):
            candidate = self.writable_path(path.relative, root=path.area, subtree=True)
            if candidate.is_dir():
                candidate.rmdir()
            else:
                self.require_file(path.relative, path.area).unlink()
            return PathOperationResult(
                path=path,
                action="deleted",
            )

    def remove_directory(self, path: WorkspacePath) -> None:
        """Remove a whole directory after checking roots, protections, and links."""
        with filesystem_errors(path):
            candidate = self.writable_path(
                path.relative,
                root=path.area,
                subtree=True,
            )
            if not candidate.exists():
                return
            self.check_tree_links(candidate)
            shutil.rmtree(candidate)

    def mkdir(
        self,
        path: WorkspacePath,
    ) -> PathOperationResult:
        with filesystem_errors(path):
            candidate = self.writable_path(path.relative, root=path.area)
            existed = candidate.is_dir()
            candidate.mkdir(parents=True, exist_ok=True)
            return PathOperationResult(
                path=path,
                action="already_exists" if existed else "created",
            )

    def validate_directory_key(self, key: str) -> None:
        if not key or key in (".", "..") or any(char in key for char in "/\\\0"):
            raise WorkspaceError("Directory key must be one nonempty directory name.")

    def storage_directory(self, key: str) -> StorageDirectory:
        self.validate_directory_key(key)
        self.mkdir(self.qualified_path("storage", key))
        return StorageDirectory(self, self.storage_root / key)

    @contextmanager
    def temporary_directory(self, prefix: str = "tmp") -> Generator[StorageDirectory]:
        self.validate_directory_key(prefix)
        parent = self.storage_directory("tmp")
        with filesystem_errors("tmp"):
            path = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=parent.path))
        directory = StorageDirectory(self, path)
        try:
            yield directory
        finally:
            directory.remove()

    def file_uri(self, path: WorkspacePath) -> str:
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
        async def workspace_list(
            ctx: Context,
            path: WorkspacePath = WorkspacePath("project/"),
            depth: int | None = 1,
            include_hidden: bool = False,
        ) -> DirectoryTree:
            """Show a directory tree; null depth expands every directory. File paths are errors."""
            report_progress = progress(ctx, interval=self.progress_interval)
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
                listed_paths = [path]

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
                        listed_paths.append(entry.path)
                        visited += 1
                        await report_progress(visited, message=f"process {entry.path}")
                        await asyncio.sleep(0)
                    return result

                with filesystem_errors(path):
                    result = DirectoryTree(
                        path=path,
                        entries=await entries(directory, depth),
                    )
                return await self.enrich_result("workspace_list", result, listed_paths)
            except WorkspaceError as error:
                raise ToolError(str(error)) from error

        @apps.tool(
            resource_uri=self.TREE_WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def workspace_find_files(
            ctx: Context,
            pattern: str = "*",
            path: WorkspacePath = WorkspacePath("project/"),
        ) -> FilePaths:
            """Find file paths by glob, skipping dot directories and links."""
            report_progress = progress(ctx, interval=self.progress_interval)
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
                return await self.enrich_result("workspace_find_files", result, paths)
            except WorkspaceError as error:
                raise ToolError(str(error)) from error

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.FILE_ICON.icon],
            annotations=read_only,
        )
        async def workspace_file_info(
            path: WorkspacePath,
            ctx: Context,
        ) -> FileInfo:
            """Read creation/modification times, byte size, and owner of one file."""
            report_progress = progress(ctx, interval=self.progress_interval)
            await report_progress(0, total=1, message="Starting file info")
            try:
                result = self.file_info(
                    path=path,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
            await report_progress(1, total=1, message="Completed file info")
            return await self.enrich_result("workspace_file_info", result, [path])

        @apps.tool(
            resource_uri=self.FILE_WIDGET.uri,
            icons=[self.FILE_ICON.icon],
            annotations=read_only,
        )
        async def workspace_read_file(
            path: WorkspacePath,
            ctx: Context,
            start_line: int = 1,
            end_line: int | None = None,
        ) -> FileContent:
            """Read UTF-8 text, optionally selecting an inclusive range of one-based lines."""
            report_progress = progress(ctx, interval=self.progress_interval)
            try:
                if start_line < 1 or (end_line is not None and end_line < start_line):
                    raise WorkspaceError(
                        "Line range must start at 1 or later and end at or after its start."
                    )
                with filesystem_errors(path):
                    candidate = self.require_file(path.relative, path.area)
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
                            f"{path}: binary file; use the raw resource."
                        )
                    result = FileContent(
                        path=path,
                        text="".join(
                            decoded.splitlines(keepends=True)[start_line - 1 : end_line]
                        ),
                        start_line=start_line,
                    )
                return await self.enrich_result(
                    "workspace_read_file",
                    result,
                    [path],
                    {path: decoded},
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error

        @apps.tool(
            resource_uri=self.SEARCH_WIDGET.uri,
            icons=[self.SEARCH_ICON.icon],
            annotations=read_only,
        )
        async def workspace_search(
            query: str,
            ctx: Context,
            path: WorkspacePath = WorkspacePath("project/"),
            regex: bool = False,
            extensions: list[str] | None = None,
            case_sensitive: bool = True,
        ) -> SearchResult:
            """Search lines by literal text or regex; report skipped binary/non-UTF-8 files."""
            report_progress = progress(ctx, interval=self.progress_interval)
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
                collect_texts = bool(self.extensions_for("workspace_search"))
                texts: dict[WorkspacePath, str] = {}
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
                                if collect_texts:
                                    texts[relative] = text
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
                return await self.enrich_result(
                    "workspace_search",
                    result,
                    [*(match.path for match in result.matches), *result.skipped_files],
                    texts,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        async def workspace_write_file(
            path: WorkspacePath,
            text: str,
            ctx: Context,
        ) -> FileWriteResult:
            """Create or overwrite a UTF-8 file; report removed and added line counts."""
            report_progress = progress(ctx, interval=self.progress_interval)
            await report_progress(0, total=1, message="Starting write file")
            changes: list[TextChange] = []
            try:
                result = self.write_file(
                    path=path,
                    text=text,
                    on_change=changes.append,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
            await report_progress(1, total=1, message="Completed write file")
            return await self.enrich_result(
                "workspace_write_file",
                result,
                [path],
                {path: text},
                change=changes[0],
            )

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        async def workspace_edit_file(
            path: WorkspacePath,
            old_text: str,
            new_text: str,
            ctx: Context,
            replace_all: bool = False,
        ) -> FileEditResult:
            """Replace one exact text occurrence, or all occurrences with replace_all=true."""
            report_progress = progress(ctx, interval=self.progress_interval)
            await report_progress(0, total=1, message="Starting edit file")
            changes: list[TextChange] = []
            try:
                result = self.edit_file(
                    path=path,
                    old_text=old_text,
                    new_text=new_text,
                    replace_all=replace_all,
                    on_change=changes.append,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
            await report_progress(1, total=1, message="Completed edit file")
            return await self.enrich_result(
                "workspace_edit_file",
                result,
                [path],
                change=changes[0],
            )

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        async def workspace_move(
            source: WorkspacePath,
            destination: WorkspacePath,
            ctx: Context,
        ) -> PathOperationResult:
            """Move a file or directory inside one root; the destination must not exist."""
            report_progress = progress(ctx, interval=self.progress_interval)
            await report_progress(0, total=1, message="Starting move")
            try:
                result = self.move(
                    source=source,
                    destination=destination,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
            await report_progress(1, total=1, message="Completed move")
            return await self.enrich_result(
                "workspace_move",
                result,
                [source, destination],
            )

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        async def workspace_delete(
            path: WorkspacePath,
            ctx: Context,
        ) -> PathOperationResult:
            """Delete a file or empty directory; protected paths and roots cannot be deleted."""
            report_progress = progress(ctx, interval=self.progress_interval)
            await report_progress(0, total=1, message="Starting delete")
            try:
                result = self.delete(
                    path=path,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
            await report_progress(1, total=1, message="Completed delete")
            return await self.enrich_result("workspace_delete", result, [path])

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
        async def workspace_mkdir(
            path: WorkspacePath,
            ctx: Context,
        ) -> PathOperationResult:
            """Create a directory and missing parents, or report that it already exists."""
            report_progress = progress(ctx, interval=self.progress_interval)
            await report_progress(0, total=1, message="Starting mkdir")
            try:
                result = self.mkdir(
                    path=path,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
            await report_progress(1, total=1, message="Completed mkdir")
            return await self.enrich_result("workspace_mkdir", result, [path])

        for widget in (
            self.TREE_WIDGET,
            self.FILE_WIDGET,
            self.SEARCH_WIDGET,
            self.RESULT_WIDGET,
        ):
            apps.add_html_resource(widget.uri, widget.content)

        def resource[T](operation: Callable[..., T], **kwargs: object) -> T:
            try:
                return operation(**kwargs)
            except (WorkspaceError, ToolError) as error:
                raise ResourceError(str(error)) from error

        @mcp.resource(
            self.RESULT_JSON_URI,
            mime_type="application/json",
            icons=[self.FILE_ICON.icon],
        )
        async def workspace_result_json(result_id: str, name: str) -> str:
            """Read immutable result extensions or provider JSON from one tool invocation."""
            try:
                return self.read_result_resource(result_id, name + ".json")
            except WorkspaceError as error:
                raise ResourceNotFoundError(str(error)) from error

        @mcp.resource(
            self.RESULT_MARKDOWN_URI,
            mime_type="text/markdown",
            icons=[self.FILE_ICON.icon],
        )
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
        async def workspace_file_resource(
            path: list[str] | None = None,
        ) -> str:
            """Read the complete UTF-8 file without formatting or metadata."""
            selected = resource_path("/".join(path or []))
            return resource(self.read_file, path=selected).text

        @mcp.resource(
            self.RAW_URI,
            mime_type="application/octet-stream",
            icons=[self.FILE_ICON.icon],
        )
        async def workspace_raw_resource(
            path: list[str] | None = None,
        ) -> bytes:
            """Read the exact bytes of any file."""
            selected = resource_path("/".join(path or []))
            return resource(self.read_bytes, path=selected)

        @mcp.resource(self.LIST_URI, mime_type="text/markdown", icons=[self.ICON.icon])
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
        async def workspace_file_info_resource(path: str = "") -> str:
            """Read one file's metadata as a Markdown table; path is required."""
            selected = resource_path(path)
            return self.render_markdown(resource(self.file_info, path=selected))

        @mcp.resource(
            self.SEARCH_URI,
            mime_type="text/markdown",
            icons=[self.SEARCH_ICON.icon],
            security=ResourceSecurity(exempt_params={"query"}),
        )
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

        complete.add_completion(workspace_completion)
