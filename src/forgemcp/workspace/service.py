"""Workspace files, storage directories, and MCP registration."""

from __future__ import annotations

import asyncio
import fnmatch
import math
import os
import shutil
import stat
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from time import monotonic
from typing import Generator, Literal
from urllib.parse import quote

import regex as regex_engine
from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
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

from .errors import WorkspaceError
from .metadata import file_owner

WorkspaceRoot = Literal["project", "storage"]


class TreeEntry(BaseModel):
    path: str
    kind: Literal["file", "directory", "symlink", "other"]
    children: list[TreeEntry] | None = None


class DirectoryTree(BaseModel):
    root: WorkspaceRoot
    path: str
    entries: list[TreeEntry]


class FilePaths(BaseModel):
    root: WorkspaceRoot
    paths: list[str]


class FileContent(BaseModel):
    root: WorkspaceRoot
    path: str
    text: str
    start_line: int


class FileInfo(BaseModel):
    root: WorkspaceRoot
    path: str
    created_at: datetime | None
    modified_at: datetime
    size_bytes: int
    owner: str | None


class SearchMatch(BaseModel):
    path: str
    line: int
    text: str
    spans: list[tuple[int, int]] = Field(
        description="Match ranges as zero-based Unicode code point [start, end) pairs.",
    )


class SearchResult(BaseModel):
    root: WorkspaceRoot
    matches: list[SearchMatch]
    skipped_files: list[str]


class FileWriteResult(BaseModel):
    root: WorkspaceRoot
    path: str
    action: Literal["created", "overwritten"]
    lines_removed: int
    lines_added: int


class FileEditResult(BaseModel):
    root: WorkspaceRoot
    path: str
    replacements: int


class PathOperationResult(BaseModel):
    root: WorkspaceRoot
    path: str
    action: Literal["moved", "deleted", "created", "already_exists"]
    source: str | None = None


@contextmanager
def filesystem_errors(path: str) -> Generator[None]:
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
        self.workspace.mkdir(relative, root="storage")
        return StorageDirectory(self.workspace, self.path / key)

    def remove(self) -> None:
        relative = self.workspace.relative_path(self.path, root="storage")
        path = self.workspace.writable_path(relative, root="storage", subtree=True)
        with filesystem_errors(relative):
            if not path.exists():
                return
            self.workspace.check_tree_links(path)
            shutil.rmtree(path)


class WorkspaceService:
    """Browse project/storage trees, search files, and read or change UTF-8 text.

    Paths are relative to root=project or root=storage. Searches skip dot directories
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
    FILE_URI = "forgemcp://workspace/{root}/file{/path*}"
    RAW_URI = "forgemcp://workspace/{root}/raw{/path*}"
    LIST_URI = "forgemcp://workspace/{root}/list{?path,depth,include_hidden}"
    FIND_URI = "forgemcp://workspace/{root}/find-files{?pattern,path}"
    INFO_URI = "forgemcp://workspace/{root}/file-info{?path}"
    SEARCH_URI = "forgemcp://workspace/{root}/search{?query,path,regex,extensions,case_sensitive}"

    def __init__(
        self,
        workspace_root: Path,
        storage_root: Path | None = None,
        *,
        progress_interval: float = 1.0,
    ) -> None:
        if not math.isfinite(progress_interval) or progress_interval < 0:
            raise WorkspaceError("Progress interval must be finite and nonnegative.")
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

    def protect_path(self, path: str, *, root: WorkspaceRoot = "project") -> None:
        candidate = self.resolve_path(path, root=root)
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

    def read_bytes(self, path: str, *, root: WorkspaceRoot = "project") -> bytes:
        with filesystem_errors(path):
            return self.require_file(path, root).read_bytes()

    def read_file(
        self,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
        root: WorkspaceRoot = "project",
    ) -> FileContent:
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise WorkspaceError(
                "Line range must start at 1 or later and end at or after its start."
            )
        with filesystem_errors(path):
            text = self.read_bytes(path, root=root).decode("utf-8")
            if "\0" in text:
                raise WorkspaceError(f"{path}: binary file; use the raw resource.")
        return FileContent(
            root=root,
            path=self.relative_path(self.resolve_path(path, root=root), root=root),
            text="".join(text.splitlines(keepends=True)[start_line - 1 : end_line]),
            start_line=start_line,
        )

    def file_info(self, path: str, *, root: WorkspaceRoot = "project") -> FileInfo:
        with filesystem_errors(path):
            candidate = self.require_file(path, root)
            metadata = candidate.stat()
            birth = getattr(metadata, "st_birthtime", None)
            return FileInfo(
                root=root,
                path=self.relative_path(candidate, root=root),
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
        path: str,
        text: str,
        *,
        root: WorkspaceRoot = "project",
    ) -> FileWriteResult:
        with filesystem_errors(path):
            candidate = self.writable_path(path, root=root)
            existed = candidate.exists()
            previous = self.read_file(path, root=root).text if existed else ""
            self.replace_text(candidate, text)
            return FileWriteResult(
                root=root,
                path=self.relative_path(candidate, root=root),
                action="overwritten" if existed else "created",
                lines_removed=len(previous.splitlines()),
                lines_added=len(text.splitlines()),
            )

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        *,
        replace_all: bool = False,
        root: WorkspaceRoot = "project",
    ) -> FileEditResult:
        if not old_text:
            raise WorkspaceError("old_text must not be empty.")
        with filesystem_errors(path):
            candidate = self.writable_path(path, root=root)
            text = self.read_file(path, root=root).text
            count = text.count(old_text)
            if count == 0:
                raise WorkspaceError(
                    "Exact text was not found; the file was not changed."
                )
            if count > 1 and not replace_all:
                raise WorkspaceError(
                    f"Found {count} occurrences; use replace_all or a more specific old_text."
                )
            self.replace_text(candidate, text.replace(old_text, new_text))
            return FileEditResult(
                root=root,
                path=self.relative_path(candidate, root=root),
                replacements=count,
            )

    def move(
        self,
        source: str,
        destination: str,
        *,
        root: WorkspaceRoot = "project",
    ) -> PathOperationResult:
        with filesystem_errors(source):
            origin = self.writable_path(source, root=root, subtree=True)
            target = self.writable_path(destination, root=root, subtree=True)
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
                root=root,
                path=self.relative_path(target, root=root),
                action="moved",
                source=self.relative_path(origin, root=root),
            )

    def delete(
        self,
        path: str,
        *,
        root: WorkspaceRoot = "project",
    ) -> PathOperationResult:
        with filesystem_errors(path):
            candidate = self.writable_path(path, root=root, subtree=True)
            if candidate.is_dir():
                candidate.rmdir()
            else:
                self.require_file(path, root).unlink()
            return PathOperationResult(
                root=root,
                path=self.relative_path(candidate, root=root),
                action="deleted",
            )

    def mkdir(
        self,
        path: str,
        *,
        root: WorkspaceRoot = "project",
    ) -> PathOperationResult:
        with filesystem_errors(path):
            candidate = self.writable_path(path, root=root)
            existed = candidate.is_dir()
            candidate.mkdir(parents=True, exist_ok=True)
            return PathOperationResult(
                root=root,
                path=self.relative_path(candidate, root=root),
                action="already_exists" if existed else "created",
            )

    def validate_directory_key(self, key: str) -> None:
        if not key or key in (".", "..") or any(char in key for char in "/\\\0"):
            raise WorkspaceError("Directory key must be one nonempty directory name.")

    def storage_directory(self, key: str) -> StorageDirectory:
        self.validate_directory_key(key)
        self.mkdir(key, root="storage")
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

    def file_uri(self, root: WorkspaceRoot, path: str) -> str:
        return f"forgemcp://workspace/{root}/file/{quote(path, safe='/')}"

    def render_markdown(
        self,
        result: DirectoryTree | FilePaths | FileInfo | SearchResult,
    ) -> str:
        """Render the same business results served by tools as Markdown resources."""
        nodes: list[markdown.Node] = []
        if isinstance(result, DirectoryTree):
            lines = [result.path]

            def visit(entries: list[TreeEntry], prefix: str = "") -> None:
                for index, entry in enumerate(entries):
                    last = index == len(entries) - 1
                    suffix = (
                        "/"
                        if entry.kind == "directory"
                        else " @" if entry.kind == "symlink" else ""
                    )
                    lines.append(
                        f"{prefix}{'└── ' if last else '├── '}{PurePosixPath(entry.path).name}{suffix}"
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
                        markdown.Link(path, self.file_uri(result.root, path))
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
                            f"{markdown.Link(match.path, self.file_uri(result.root, match.path))}, line {match.line}"
                        ),
                        markdown.CodeBlock(match.text),
                    ]
                )
            if not result.matches:
                nodes.append(markdown.Paragraph("No matches."))
            nodes.extend(
                [
                    markdown.Heading("Skipped files", level=2),
                    markdown.UnorderedList(result.skipped_files),
                ]
            )
        return markdown.Document(nodes).render()

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        """Register file tools, mirrors, Markdown resources, and completions."""
        def progress(ctx: Context) -> Callable[..., Awaitable[None]]:
            last_sent: float | None = None

            async def report(
                value: float,
                total: float | None = None,
                message: str | None = None,
            ) -> None:
                nonlocal last_sent
                now = monotonic()
                if last_sent is not None and now - last_sent < self.progress_interval:
                    return
                last_sent = now
                await ctx.report_progress(value, total=total, message=message)

            return report

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
            path: str = ".",
            depth: int | None = 1,
            include_hidden: bool = False,
            root: WorkspaceRoot = "project",
        ) -> DirectoryTree:
            """Show a directory tree; null depth expands every directory. File paths are errors."""
            report_progress = progress(ctx)
            try:
                visited = 0
                await report_progress(0, message="Starting directory scan")
                if depth is not None and depth < 1:
                    raise WorkspaceError(
                        "Depth must be positive or null for the complete tree."
                    )
                directory = self.resolve_path(path, root=root)
                self.check_path_links(directory, root)
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
                            path=child.relative_to(self.root_path(root)).as_posix(),
                            kind=kind,
                            children=children,
                        )
                        result.append(entry)
                        visited += 1
                        await report_progress(visited, message=f"process {entry.path}")
                        await asyncio.sleep(0)
                    return result

                with filesystem_errors(path):
                    return DirectoryTree(
                        root=root,
                        path=self.relative_path(directory, root=root),
                        entries=await entries(directory, depth),
                    )
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
            path: str = ".",
            root: WorkspaceRoot = "project",
        ) -> FilePaths:
            """Find file paths by glob, skipping dot directories and links."""
            report_progress = progress(ctx)
            try:
                visited = 0
                await report_progress(0, message="Files visited")
                start = self.resolve_path(path, root=root)
                base = start if start.is_dir() else start.parent
                if not pattern:
                    raise WorkspaceError("File pattern must not be empty.")
                paths = []
                with filesystem_errors(path):
                    async for candidate in self.iter_files(path, root):
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
                            paths.append(self.relative_path(candidate, root=root))
                return FilePaths(root=root, paths=sorted(paths))
            except WorkspaceError as error:
                raise ToolError(str(error)) from error

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.FILE_ICON.icon],
            annotations=read_only,
        )
        async def workspace_file_info(
            path: str,
            ctx: Context,
            root: WorkspaceRoot = "project",
        ) -> FileInfo:
            """Read creation/modification times, byte size, and owner of one file."""
            report_progress = progress(ctx)
            await report_progress(0, total=1, message="Starting file info")
            try:
                result = self.file_info(
                    path=path,
                    root=root,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
            await report_progress(1, total=1, message="Completed file info")
            return result

        @apps.tool(
            resource_uri=self.FILE_WIDGET.uri,
            icons=[self.FILE_ICON.icon],
            annotations=read_only,
        )
        async def workspace_read_file(
            path: str,
            ctx: Context,
            start_line: int = 1,
            end_line: int | None = None,
            root: WorkspaceRoot = "project",
        ) -> FileContent:
            """Read UTF-8 text, optionally selecting an inclusive range of one-based lines."""
            report_progress = progress(ctx)
            try:
                if start_line < 1 or (end_line is not None and end_line < start_line):
                    raise WorkspaceError(
                        "Line range must start at 1 or later and end at or after its start."
                    )
                with filesystem_errors(path):
                    candidate = self.require_file(path, root)
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
                    return FileContent(
                        root=root,
                        path=self.relative_path(candidate, root=root),
                        text="".join(
                            decoded.splitlines(keepends=True)[start_line - 1 : end_line]
                        ),
                        start_line=start_line,
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
            path: str = ".",
            regex: bool = False,
            extensions: list[str] | None = None,
            case_sensitive: bool = True,
            root: WorkspaceRoot = "project",
        ) -> SearchResult:
            """Search lines by literal text or regex; report skipped binary/non-UTF-8 files."""
            report_progress = progress(ctx)
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
                    async for candidate in self.iter_files(path, root):
                        visited += 1
                        await report_progress(visited, message="Files visited")
                        if not self.extension_matches(candidate, extensions):
                            continue
                        relative = self.relative_path(candidate, root=root)
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
                return SearchResult(
                    root=root,
                    matches=sorted(matches, key=lambda item: (item.path, item.line)),
                    skipped_files=sorted(skipped),
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        async def workspace_write_file(
            path: str,
            text: str,
            ctx: Context,
            root: WorkspaceRoot = "project",
        ) -> FileWriteResult:
            """Create or overwrite a UTF-8 file; report removed and added line counts."""
            report_progress = progress(ctx)
            await report_progress(0, total=1, message="Starting write file")
            try:
                result = self.write_file(
                    path=path,
                    text=text,
                    root=root,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
            await report_progress(1, total=1, message="Completed write file")
            return result

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        async def workspace_edit_file(
            path: str,
            old_text: str,
            new_text: str,
            ctx: Context,
            replace_all: bool = False,
            root: WorkspaceRoot = "project",
        ) -> FileEditResult:
            """Replace one exact text occurrence, or all occurrences with replace_all=true."""
            report_progress = progress(ctx)
            await report_progress(0, total=1, message="Starting edit file")
            try:
                result = self.edit_file(
                    path=path,
                    old_text=old_text,
                    new_text=new_text,
                    replace_all=replace_all,
                    root=root,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
            await report_progress(1, total=1, message="Completed edit file")
            return result

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        async def workspace_move(
            source: str,
            destination: str,
            ctx: Context,
            root: WorkspaceRoot = "project",
        ) -> PathOperationResult:
            """Move a file or directory inside one root; the destination must not exist."""
            report_progress = progress(ctx)
            await report_progress(0, total=1, message="Starting move")
            try:
                result = self.move(
                    source=source,
                    destination=destination,
                    root=root,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
            await report_progress(1, total=1, message="Completed move")
            return result

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        async def workspace_delete(
            path: str,
            ctx: Context,
            root: WorkspaceRoot = "project",
        ) -> PathOperationResult:
            """Delete a file or empty directory; protected paths and roots cannot be deleted."""
            report_progress = progress(ctx)
            await report_progress(0, total=1, message="Starting delete")
            try:
                result = self.delete(
                    path=path,
                    root=root,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
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
        async def workspace_mkdir(
            path: str,
            ctx: Context,
            root: WorkspaceRoot = "project",
        ) -> PathOperationResult:
            """Create a directory and missing parents, or report that it already exists."""
            report_progress = progress(ctx)
            await report_progress(0, total=1, message="Starting mkdir")
            try:
                result = self.mkdir(
                    path=path,
                    root=root,
                )
            except WorkspaceError as error:
                raise ToolError(str(error)) from error
            await report_progress(1, total=1, message="Completed mkdir")
            return result

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
            self.FILE_URI,
            mime_type="text/plain",
            icons=[self.FILE_ICON.icon],
        )
        async def workspace_file_resource(
            root: str,
            path: list[str] | None = None,
        ) -> str:
            """Read the complete UTF-8 file without formatting or metadata."""
            resource(self.root_path, root=root)
            return resource(self.read_file, path="/".join(path or []), root=root).text

        @mcp.resource(
            self.RAW_URI,
            mime_type="application/octet-stream",
            icons=[self.FILE_ICON.icon],
        )
        async def workspace_raw_resource(
            root: str,
            path: list[str] | None = None,
        ) -> bytes:
            """Read the exact bytes of any file."""
            resource(self.root_path, root=root)
            return resource(self.read_bytes, path="/".join(path or []), root=root)

        @mcp.resource(self.LIST_URI, mime_type="text/markdown", icons=[self.ICON.icon])
        async def workspace_list_resource(
            root: str,
            ctx: Context,
            path: str = ".",
            depth: int | Literal["all"] = 1,
            include_hidden: bool = False,
        ) -> str:
            """Read a directory tree as Markdown; depth=all expands the whole tree."""
            try:
                self.root_path(root)
                result = await workspace_list(
                    ctx=ctx,
                    root=root,
                    path=path,
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
            root: str,
            ctx: Context,
            pattern: str = "*",
            path: str = ".",
        ) -> str:
            """Read matching file links as Markdown."""
            try:
                self.root_path(root)
                result = await workspace_find_files(
                    ctx=ctx,
                    root=root,
                    pattern=pattern,
                    path=path,
                )
                return self.render_markdown(result)
            except (WorkspaceError, ToolError) as error:
                raise ResourceError(str(error)) from error

        @mcp.resource(
            self.INFO_URI,
            mime_type="text/markdown",
            icons=[self.FILE_ICON.icon],
        )
        async def workspace_file_info_resource(root: str, path: str = "") -> str:
            """Read one file's metadata as a Markdown table; path is required."""
            resource(self.root_path, root=root)
            return self.render_markdown(resource(self.file_info, root=root, path=path))

        @mcp.resource(
            self.SEARCH_URI,
            mime_type="text/markdown",
            icons=[self.SEARCH_ICON.icon],
            security=ResourceSecurity(exempt_params={"query"}),
        )
        async def workspace_search_resource(
            root: str,
            ctx: Context,
            query: str = "",
            path: str = ".",
            regex: bool = False,
            extensions: str | None = None,
            case_sensitive: bool = True,
        ) -> str:
            """Read text/regex matches and skipped files as Markdown; query is required."""
            try:
                self.root_path(root)
                result = await workspace_search(
                    ctx=ctx,
                    root=root,
                    query=query,
                    path=path,
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
            if argument.name == "root":
                values = ["project", "storage"]
            elif argument.name in ("regex", "case_sensitive", "include_hidden"):
                values = ["false", "true"]
            elif argument.name == "depth":
                values = ["1", "2", "3", "all"]
            elif argument.name in ("path", "extensions"):
                selected = (
                    (context.arguments or {}).get("root", "project")
                    if context
                    else "project"
                )
                try:
                    if argument.name == "extensions" and ref.uri != self.SEARCH_URI:
                        return Completion(values=[])
                    if argument.name == "extensions":
                        extensions = sorted(
                            {
                                candidate.suffix.removeprefix(".")
                                async for candidate in self.iter_files(".", selected)
                            }
                        )
                        prefix, separator, tail = argument.value.rpartition(",")
                        values = [
                            prefix + separator + value
                            for value in extensions
                            if value.startswith(tail)
                        ]
                    else:
                        parent = (
                            argument.value.replace("\\", "/").rpartition("/")[0] or "."
                        )
                        directory = self.resolve_path(parent, root=selected)
                        self.check_path_links(directory, selected)
                        with filesystem_errors(parent):
                            for child in sorted(directory.iterdir()):
                                await asyncio.sleep(0)
                                if self.is_link(child):
                                    continue
                                is_directory = child.is_dir()
                                if ref.uri == self.LIST_URI and not is_directory:
                                    continue
                                if is_directory or child.is_file():
                                    values.append(
                                        self.relative_path(child, root=selected)
                                        + ("/" if is_directory else "")
                                    )
                except (WorkspaceError, ToolError):
                    values = []
            matches = [value for value in values if value.startswith(argument.value)]
            return Completion(
                values=matches[:100],
                total=len(matches),
                has_more=len(matches) > 100,
            )

        complete.add_completion(workspace_completion)
