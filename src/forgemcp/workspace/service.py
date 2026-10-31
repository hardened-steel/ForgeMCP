"""Workspace files, storage directories, and MCP registration."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import errno
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
    CallToolResult,
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
from forgemcp.diagnostics import display_value, os_error_reason
from forgemcp.progress import progress
from forgemcp.text import tool_result

from .errors import WorkspaceError
from .providers import ProviderCall, ResultResource, ResultResources
from .metadata import file_owner
from .path import WorkspacePath
from .models import (
    TreeEntry,
    DirectoryTree,
    FoundFile,
    FilePaths,
    FileContent,
    FileInfo,
    SearchMatch,
    SearchResult,
    FileWriteResult,
    FileEditResult,
    PathOperationResult,
)
from .text import render_text


class ReadConfirmation(BaseModel):
    """The operator's decision to allow reading an external file."""

    allow: bool = Field(description="Allow reading this external file.")


async def confirm_read(path: WorkspacePath) -> ReadConfirmation | Elicit[ReadConfirmation]:
    """Allow managed reads directly and request SDK elicitation for external reads."""
    if path.area != "root":
        return ReadConfirmation(allow=True)
    if not Path(path.relative).is_absolute():
        example = "root/C:/SDK/header.h" if os.name == "nt" else "root//usr/include/header.h"
        raise ToolError(
            f"Invalid path={display_value(path)}: the external path is not absolute on this "
            f"host. Use an absolute path for the server's OS, for example {display_value(example)}."
        )
    return Elicit(f"Allow reading external file {path.absolute}?", ReadConfirmation)


WorkspaceRoot = Literal["project", "storage"]


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
        """Opt out of workspace_list enrichment unless a provider overrides this hook."""
        return None

    async def after_workspace_list(
        self,
        call_id: str,
        context: ContextT,
        result: DirectoryTree,
    ) -> str | None:
        """Return no linked resource unless a provider handles the completed workspace_list call."""
        return None

    async def before_workspace_find_files(
        self,
        call_id: str,
        pattern: str,
        path: WorkspacePath,
    ) -> ContextT | None:
        """Opt out of workspace_find_files enrichment unless a provider overrides this hook."""
        return None

    async def after_workspace_find_files(
        self,
        call_id: str,
        context: ContextT,
        result: FilePaths,
    ) -> str | None:
        """Return no linked resource unless a provider handles the completed workspace_find_files
        call.
        """
        return None

    async def before_workspace_file_info(
        self,
        call_id: str,
        path: WorkspacePath,
    ) -> ContextT | None:
        """Opt out of workspace_file_info enrichment unless a provider overrides this hook."""
        return None

    async def after_workspace_file_info(
        self,
        call_id: str,
        context: ContextT,
        result: FileInfo,
    ) -> str | None:
        """Return no linked resource unless a provider handles the completed workspace_file_info
        call.
        """
        return None

    async def before_workspace_read_file(
        self,
        call_id: str,
        path: WorkspacePath,
        confirm: ReadConfirmation,
        start_line: int,
        end_line: int | None,
    ) -> ContextT | None:
        """Opt out of workspace_read_file enrichment unless a provider overrides this hook."""
        return None

    async def after_workspace_read_file(
        self,
        call_id: str,
        context: ContextT,
        result: FileContent,
    ) -> str | None:
        """Return no linked resource unless a provider handles the completed workspace_read_file
        call.
        """
        return None

    async def before_workspace_text_search(
        self,
        call_id: str,
        query: str,
        path: WorkspacePath,
        regex: bool,
        extensions: list[str] | None,
        case_sensitive: bool,
    ) -> ContextT | None:
        """Opt out of workspace_text_search enrichment unless a provider overrides this hook."""
        return None

    async def after_workspace_text_search(
        self,
        call_id: str,
        context: ContextT,
        result: SearchResult,
    ) -> str | None:
        """Return no linked resource unless a provider handles the completed workspace_text_search
        call.
        """
        return None

    async def before_workspace_write_file(
        self,
        call_id: str,
        path: WorkspacePath,
        text: str,
    ) -> ContextT | None:
        """Opt out of workspace_write_file enrichment unless a provider overrides this hook."""
        return None

    async def after_workspace_write_file(
        self,
        call_id: str,
        context: ContextT,
        result: FileWriteResult,
    ) -> str | None:
        """Return no linked resource unless a provider handles the completed workspace_write_file
        call.
        """
        return None

    async def before_workspace_edit_file(
        self,
        call_id: str,
        path: WorkspacePath,
        old_text: str,
        new_text: str,
        replace_all: bool,
    ) -> ContextT | None:
        """Opt out of workspace_edit_file enrichment unless a provider overrides this hook."""
        return None

    async def after_workspace_edit_file(
        self,
        call_id: str,
        context: ContextT,
        result: FileEditResult,
    ) -> str | None:
        """Return no linked resource unless a provider handles the completed workspace_edit_file
        call.
        """
        return None

    async def before_workspace_move(
        self,
        call_id: str,
        source: WorkspacePath,
        destination: WorkspacePath,
    ) -> ContextT | None:
        """Opt out of workspace_move enrichment unless a provider overrides this hook."""
        return None

    async def after_workspace_move(
        self,
        call_id: str,
        context: ContextT,
        result: PathOperationResult,
    ) -> str | None:
        """Return no linked resource unless a provider handles the completed workspace_move call."""
        return None

    async def before_workspace_delete(
        self,
        call_id: str,
        path: WorkspacePath,
    ) -> ContextT | None:
        """Opt out of workspace_delete enrichment unless a provider overrides this hook."""
        return None

    async def after_workspace_delete(
        self,
        call_id: str,
        context: ContextT,
        result: PathOperationResult,
    ) -> str | None:
        """Return no linked resource unless a provider handles the completed workspace_delete
        call.
        """
        return None

    async def before_workspace_mkdir(
        self,
        call_id: str,
        path: WorkspacePath,
    ) -> ContextT | None:
        """Opt out of workspace_mkdir enrichment unless a provider overrides this hook."""
        return None

    async def after_workspace_mkdir(
        self,
        call_id: str,
        context: ContextT,
        result: PathOperationResult,
    ) -> str | None:
        """Return no linked resource unless a provider handles the completed workspace_mkdir
        call.
        """
        return None

    async def error(self, call_id: str, context: ContextT) -> None:
        """Provide a no-op cleanup hook for a failed participating workspace call."""


@contextmanager
def filesystem_errors(
    path: str | WorkspacePath,
    *,
    parameter: str = "path",
    text_parameter: str = "text",
) -> Generator[None]:
    """Translate expected failures without exposing absolute filesystem paths."""
    try:
        yield
    except UnicodeError as error:
        if isinstance(error, UnicodeEncodeError):
            raise WorkspaceError(
                f"Invalid {text_parameter}: text contains characters that cannot be encoded "
                "as UTF-8. Remove unpaired Unicode surrogates before retrying; "
                "the file was not changed."
            ) from error
        recovery = (
            "Managed file bytes can be read through the workspace raw resource."
            if str(path).startswith(("project/", "storage/"))
            else "Choose a UTF-8 text file."
        )
        raise WorkspaceError(
            f"Invalid {parameter}={display_value(path)}: expected valid UTF-8 text. "
            f"{recovery}"
        ) from error
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR):
            recovery = (
                "Inspect the path and its parent with workspace_list; file writes and moves "
                "require an existing parent directory, which workspace_mkdir can create."
            )
        elif error.errno in (errno.EACCES, errno.EPERM) or isinstance(error, PermissionError):
            recovery = (
                "Check filesystem permissions and whether another process has locked the path."
            )
        elif error.errno == errno.ENOTEMPTY:
            recovery = (
                "workspace_delete removes only files and empty directories; "
                "inspect the directory with workspace_list before choosing individual entries."
            )
        elif error.errno == errno.EISDIR:
            recovery = "Choose a regular file; inspect this directory with workspace_list."
        elif error.errno == errno.EEXIST:
            recovery = (
                "Choose an unused destination or inspect the existing path with workspace_list."
            )
        else:
            recovery = "Check the path, filesystem permissions, and whether the path is in use."
        raise WorkspaceError(
            f"Invalid {parameter}={display_value(path)}: {os_error_reason(error)}. {recovery}"
        ) from error


def read_text(candidate: Path, path: WorkspacePath) -> str:
    """Read complete UTF-8 text without newline conversion for Workspace and diff."""
    with filesystem_errors(path):
        if not candidate.is_file():
            raise WorkspaceError(
                f"Invalid path={display_value(path)}: expected an existing regular file. "
                "Use workspace_list to inspect directories or workspace_find_files "
                "to locate a file."
            )
        text = candidate.read_bytes().decode("utf-8")
        if "\0" in text:
            raise WorkspaceError(
                f"Invalid path={display_value(path)}: expected UTF-8 text, found a binary file. "
                "Use the workspace raw resource to read managed file bytes."
            )
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
    SEARCH_URI = (
        "forgemcp://workspace/search"
        "{?query,path,regex,extensions,case_sensitive,max_matches}"
    )
    SKIPPED_FILES_RESOURCE = "skipped_files"
    RESULT_JSON_URI = "forgemcp://workspace/results/{result_id}/{name}.json"
    RESULT_MARKDOWN_URI = "forgemcp://workspace/results/{result_id}/{name}.md"
    PROVIDER_TOOLS = frozenset(
        (
            "workspace_list",
            "workspace_find_files",
            "workspace_file_info",
            "workspace_read_file",
            "workspace_text_search",
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
        """Validate project and storage roots and initialize provider, resource, and lock state."""
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
            raise WorkspaceError(
                f"Workspace result resource result_id={display_value(result_id)}, "
                f"name={display_value(name)} does not exist in this server session. "
                "Use an exact resource URI from a current tool result; retained snapshots "
                "are lost when the server restarts."
            ) from error

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
            or name == self.SKIPPED_FILES_RESOURCE
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
        """Store an immutable provider resource or Workspace's own skipped-file list."""
        if len(call_id) != 32 or any(char not in "0123456789abcdef" for char in call_id):
            raise WorkspaceError("Invalid Workspace call ID.")
        if provider not in self.result_providers and provider != self.SKIPPED_FILES_RESOURCE:
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
            """Invoke a provider with copied arguments and retain its participating context."""
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
            """Run a participating provider with a copied result and mark its hook finished."""
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
            """Ask a participating provider to clean up after a failed call."""
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
        """Resolve a managed area name to its configured native root."""
        if root == "project":
            return self.root
        if root == "storage":
            return self.storage_root
        raise WorkspaceError(
            f"Invalid root={display_value(root)}: expected 'project' or 'storage'. "
            "Use a qualified managed path such as 'project/src/main.cpp' or 'storage/build/debug'."
        )

    def resolve_path(
        self,
        path: str,
        *,
        root: WorkspaceRoot = "project",
        parameter: str = "path",
    ) -> Path:
        """Return a checked absolute path, retaining its lexical link components."""
        requested = f"{root}/{'' if path == '.' else path}"
        if root not in ("project", "storage"):
            raise WorkspaceError(
                f"Invalid {parameter}={display_value(requested)}: this operation accepts only "
                "project/ or storage/ paths. External root/ files can only be read with "
                "workspace_read_file and operator approval; external writes are forbidden."
            )
        if not path or "\0" in path:
            raise WorkspaceError(
                f"Invalid {parameter}={display_value(requested)}: use a nonempty relative path "
                "without NUL characters, for example 'project/src/main.cpp'."
            )
        portable = PurePosixPath(path.replace("\\", "/"))
        windows = PureWindowsPath(path)
        if (
            portable.is_absolute()
            or windows.drive
            or windows.root
            or ".." in portable.parts
        ):
            raise WorkspaceError(
                f"Invalid {parameter}={display_value(requested)}: managed paths must be "
                "relative and cannot contain '..'. Use a path within project/ or storage/."
            )
        if os.name == "nt" and any(os.path.isreserved(part) for part in portable.parts):
            reserved = next(part for part in portable.parts if os.path.isreserved(part))
            raise WorkspaceError(
                f"Invalid {parameter}={display_value(requested)}: component "
                f"{display_value(reserved)} is reserved on Windows. Choose a different filename."
            )
        base = self.root_path(root)
        candidate = base.joinpath(*portable.parts)
        with filesystem_errors(requested, parameter=parameter):
            if not candidate.resolve().is_relative_to(base):
                raise WorkspaceError(
                    f"Invalid {parameter}={display_value(requested)}: the path resolves outside "
                    "its managed root. Choose a path within that root without an escaping link."
                )
        return candidate

    def relative_path(self, path: Path, *, root: WorkspaceRoot = "project") -> str:
        """Return a validated POSIX path relative to the selected managed root."""
        try:
            relative = path.relative_to(self.root_path(root)).as_posix()
        except ValueError as error:
            raise WorkspaceError(
                f"Invalid path: it is outside root={display_value(root)}. "
                "Choose a path within the selected project/ or storage/ root."
            ) from error
        self.resolve_path(relative, root=root)
        return relative

    def is_link(self, path: Path) -> bool:
        """Recognize both symbolic links and Windows directory junctions."""
        return path.is_symlink() or path.is_junction()

    def check_path_links(
        self,
        path: Path,
        root: WorkspaceRoot,
        *,
        parameter: str = "path",
    ) -> None:
        """Reject any link encountered between a candidate path and its managed root."""
        base = self.root_path(root)
        current = path
        while True:
            if self.is_link(current):
                raise WorkspaceError(
                    f"Invalid {parameter}={display_value(self.workspace_path(path))}: "
                    f"symbolic link or junction {display_value(self.workspace_path(current))} "
                    "cannot be traversed by this operation. Choose a direct managed path."
                )
            if current == base:
                break
            current = current.parent

    def check_tree_links(self, path: Path) -> None:
        """Reject a tree containing links before moving or removing it."""
        for directory, dirs, files in os.walk(
            path,
            followlinks=False,
            onerror=self.raise_walk_error,
        ):
            if any(self.is_link(Path(directory) / name) for name in [*dirs, *files]):
                raise WorkspaceError(
                    f"Invalid source={display_value(self.workspace_path(path))}: the tree "
                    "contains symbolic links or junctions and cannot be moved or removed. "
                    "Inspect it with workspace_list and choose an entry without links."
                )

    def protect_path(self, path: WorkspacePath) -> None:
        """Protect both the declared path and its resolved target from mutations."""
        candidate = self.resolve_workspace_path(path)
        self.protected_paths.update((candidate, candidate.resolve()))

    def writable_path(
        self,
        path: str,
        *,
        root: WorkspaceRoot,
        subtree: bool = False,
        parameter: str = "path",
    ) -> Path:
        """Validate a mutation target against roots, links, reserved names, and protected paths."""
        candidate = self.resolve_path(path, root=root, parameter=parameter)
        self.check_path_links(candidate, root, parameter=parameter)
        requested = self.workspace_path(candidate)
        if candidate in (self.root, self.storage_root):
            raise WorkspaceError(
                f"Invalid {parameter}={display_value(requested)}: cannot change a workspace root. "
                "Choose a child path within project/ or storage/."
            )
        for protected in self.protected_paths:
            if candidate.is_relative_to(protected) or (
                subtree and protected.is_relative_to(candidate)
            ):
                raise WorkspaceError(
                    f"Invalid {parameter}={display_value(requested)}: this path is protected "
                    "or contains a protected descendant. Read access remains available; "
                    "choose an unprotected path for this mutation."
                )
        if subtree and self.storage_root.is_relative_to(candidate):
            raise WorkspaceError(
                f"Invalid {parameter}={display_value(requested)}: cannot move or remove a parent "
                "of the storage root. Choose a child path that does not contain storage/."
            )
        return candidate

    def resolve_workspace_path(self, path: WorkspacePath) -> Path:
        """Resolve a qualified managed path through the selected root's access checks."""
        return self.resolve_path(path.relative, root=path.area)

    def qualified_path(self, root: str, path: str) -> WorkspacePath:
        """Adapt a resource URI's root/path pair to the shared path type."""
        self.root_path(root)
        if not path:
            raise WorkspaceError(
                "Invalid path='': use a qualified path such as 'project/' "
                "or 'project/src/main.cpp'."
            )
        try:
            return WorkspacePath(f"{root}/{'' if path == '.' else path}")
        except ValueError as error:
            raise WorkspaceError(str(error)) from error

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
        """Require an existing regular file within the selected managed root."""
        candidate = self.resolve_path(path, root=root)
        if not candidate.is_file():
            found = (
                "found a directory" if candidate.is_dir() else "the file is missing or not regular"
            )
            raise WorkspaceError(
                f"Invalid path={display_value(self.workspace_path(candidate))}: expected an "
                f"existing regular file; {found}. Use workspace_list to inspect directories "
                "or workspace_find_files to locate a file."
            )
        return candidate

    def raise_walk_error(self, error: OSError) -> None:
        """Propagate filesystem traversal errors instead of silently skipping unreadable entries."""
        raise error

    async def iter_files(self, path: str, root: WorkspaceRoot) -> AsyncGenerator[Path]:
        """Yield eligible files cooperatively, excluding hidden directories and link traversal."""
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
            raise WorkspaceError(
                f"Invalid path={display_value(self.workspace_path(start))}: expected an existing "
                "file or directory. Use workspace_list(path='project/') or "
                "workspace_list(path='storage/') to inspect available paths."
            )
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
        """Match a file suffix against optional case-insensitive extensions."""
        return extensions is None or path.suffix.removeprefix(".").lower() in {
            extension.removeprefix(".").lower() for extension in extensions
        }

    def file_uri(self, path: WorkspacePath) -> str:
        """Build a quoted managed text-mirror URI and reject external paths."""
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
                """Append indented tree rows while preserving directory and link markers."""
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
                    markdown.Paragraph(f"{result.skipped_files_count} skipped files."),
                ]
            )
            nodes.append(
                markdown.Paragraph(
                    f"Returned {len(result.matches)} of {result.matches_count} matching lines; "
                    f"matches_truncated={result.matches_truncated}."
                )
            )
            nodes.extend(
                markdown.Paragraph(
                    markdown.Link(
                        "Full skipped-file list",
                        link.uri,
                    )
                )
                for name, link in result.resources.items()
                if name == self.SKIPPED_FILES_RESOURCE
            )
        return markdown.Document(nodes).render()

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        """Register file tools, mirrors, Markdown resources, and completions."""

        def serialized[**P, R](
            handler: Callable[P, Awaitable[R]],
        ) -> Callable[P, Awaitable[R]]:
            """Wrap a workspace handler in the service's reentrant operation lock."""
            @wraps(handler)
            async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
                """Execute the original handler while holding the serialized operation context."""
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
        ) -> Annotated[CallToolResult, DirectoryTree]:
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
                        f"Invalid depth={display_value(depth)}: expected an integer >= 1, "
                        "or null to expand the complete directory tree."
                    )
                directory = self.resolve_workspace_path(path)
                self.check_path_links(directory, path.area)
                if not directory.is_dir():
                    found = "found a file" if directory.is_file() else "the directory is missing"
                    raise WorkspaceError(
                        f"Invalid path={display_value(path)}: expected an existing directory; "
                        f"{found}. Use workspace_read_file for a file, or workspace_list on "
                        "its parent directory to inspect available paths."
                    )

                async def entries(
                    parent: Path,
                    remaining: int | None,
                ) -> list[TreeEntry]:
                    """Traverse eligible children to the requested depth and report visited
                    entries.
                    """
                    nonlocal visited
                    result: list[TreeEntry] = []
                    with filesystem_errors(self.workspace_path(parent)):
                        children = sorted(parent.iterdir(), key=lambda item: item.name)
                    for child in children:
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
            return tool_result(result, render_text(result))

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
        ) -> Annotated[CallToolResult, FilePaths]:
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
                    raise WorkspaceError(
                        "Invalid pattern='': expected a nonempty filename or path glob. "
                        "Use '*' for all files or '*.cpp' for C++ sources."
                    )
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
                            with filesystem_errors(found):
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
            return tool_result(
                result,
                f'Files matching "{pattern}" under {path}:\n{render_text(result)}',
            )

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.FILE_ICON.icon],
            annotations=read_only,
        )
        @serialized
        async def workspace_file_info(
            path: WorkspacePath,
            ctx: Context,
        ) -> Annotated[CallToolResult, FileInfo]:
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
            return tool_result(result, render_text(result))

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
        ) -> Annotated[CallToolResult, FileContent]:
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
                if start_line < 1:
                    raise WorkspaceError(
                        f"Invalid start_line={display_value(start_line)}: lines are numbered "
                        "from 1; use start_line=1 to read from the beginning."
                    )
                if end_line is not None and end_line < start_line:
                    raise WorkspaceError(
                        f"Invalid end_line={display_value(end_line)}: must be >= "
                        f"start_line={display_value(start_line)}, or null to read through EOF."
                    )
                with filesystem_errors(path):
                    if not confirm.allow:
                        raise WorkspaceError(
                            f"Cannot read path={display_value(path)}: external file reading "
                            "was not permitted by the operator. No file content was read; "
                            "choose a managed file or obtain operator approval "
                            "for this external read."
                        )
                    candidate = (
                        path.absolute if path.area == "root"
                        else self.require_file(path.relative, path.area)
                    )
                    if not candidate.is_file():
                        raise WorkspaceError(
                            f"Invalid path={display_value(path)}: "
                            "expected an existing regular file. Choose an existing UTF-8 file; "
                            "workspace_list can inspect managed directories."
                        )
                    text = bytearray()
                    await report_progress(0, message="Bytes read")
                    with candidate.open("rb") as stream:
                        while chunk := stream.read(64 * 1024):
                            text.extend(chunk)
                            await report_progress(len(text), message="Bytes read")
                            await asyncio.sleep(0)
                    decoded = text.decode("utf-8")
                    if "\0" in decoded:
                        recovery = (
                            "Use the workspace raw resource to read managed file bytes."
                            if path.area != "root"
                            else "Choose a UTF-8 text file; external files have no mirror resource."
                        )
                        raise WorkspaceError(
                            f"Invalid path={display_value(path)}: expected UTF-8 text, "
                            f"found a binary file. {recovery}"
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
            return tool_result(result, render_text(result))

        @apps.tool(
            resource_uri=self.SEARCH_WIDGET.uri,
            icons=[self.SEARCH_ICON.icon],
            annotations=read_only,
        )
        @serialized
        async def workspace_text_search(
            query: str,
            ctx: Context,
            path: WorkspacePath = WorkspacePath("project/"),
            regex: bool = False,
            extensions: list[str] | None = None,
            case_sensitive: bool = True,
            max_matches: Annotated[int, Field(ge=1)] = 100,
        ) -> Annotated[CallToolResult, SearchResult]:
            """Search lines by literal text or regex; report skipped binary/non-UTF-8 files.

            extensions accepts suffixes with or without a leading dot;
            null selects all files, while an empty list selects none.
            max_matches limits returned matching lines, not scanning. Counts are exact;
            skipped-file paths are available through the linked immutable resource.
            """
            report_progress = progress(ctx, interval=self.progress_interval)
            call = await self.before_providers(
                "workspace_text_search",
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
                    raise WorkspaceError(
                        "Invalid query='': expected nonempty search text. Supply literal text, "
                        "or a regular expression with regex=true; "
                        "use workspace_find_files to list files."
                    )
                if max_matches < 1:
                    raise WorkspaceError(
                        f"Invalid max_matches={display_value(max_matches)}: expected an integer "
                        ">= 1; use max_matches=100 for the default result size."
                    )
                try:
                    expression = regex_engine.compile(
                        query if regex else regex_engine.escape(query),
                        flags=regex_engine.VERSION1
                        | (0 if case_sensitive else regex_engine.IGNORECASE),
                    )
                except regex_engine.error as error:
                    raise WorkspaceError(
                        f"Invalid query regular expression: {display_value(str(error))}. "
                        "Correct the expression at the reported position, or set regex=false "
                        "to search the query literally."
                    ) from error
                matches, skipped = [], []
                matches_count = 0
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
                        with filesystem_errors(current):
                            data = candidate.read_bytes()
                        try:
                            text = data.decode("utf-8")
                        except UnicodeDecodeError:
                            skipped.append(current)
                            continue
                        if "\0" in text:
                            skipped.append(current)
                            continue
                        for line, value in enumerate(text.splitlines(), 1):
                            if line % 256 == 0:
                                await asyncio.sleep(0)
                            found = expression.search(value) is not None
                            matches_count += int(found)
                            if found and len(matches) < max_matches:
                                spans = [
                                    match.span() for match in expression.finditer(value)
                                ]
                                matches.append(
                                    SearchMatch(
                                        path=current,
                                        line=line,
                                        text=value,
                                        spans=spans,
                                    )
                                )
                result = SearchResult(
                    matches=sorted(matches, key=lambda item: (str(item.path), item.line)),
                    matches_count=matches_count,
                    matches_truncated=matches_count > len(matches),
                    skipped_files_count=len(skipped),
                )
            except BaseException as error:
                await self.error_providers(call)
                if isinstance(error, WorkspaceError):
                    raise ToolError(str(error)) from error
                raise
            result.resources = (await self.after_providers(call, result)).resources
            if skipped:
                result.resources[self.SKIPPED_FILES_RESOURCE] = ResultResource(
                    uri=self.save_result_resource(
                        call.id,
                        self.SKIPPED_FILES_RESOURCE,
                        "application/json",
                        json.dumps({"skipped_files": sorted(map(str, skipped))}),
                    ),
                    mime_type="application/json",
                )
            selected_extensions = "All" if extensions is None else ", ".join(extensions)
            return tool_result(
                result,
                f'Query: "{query}" ('
                f"{'regular expression' if regex else 'literal text'}; "
                f"{'case-sensitive' if case_sensitive else 'case-insensitive'})\n"
                f"Path: {path}\nExtensions: {selected_extensions or 'No extensions'}\n\n"
                + render_text(result),
            )

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
        ) -> Annotated[CallToolResult, FileWriteResult]:
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
            return tool_result(result, render_text(result))

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
        ) -> Annotated[CallToolResult, FileEditResult]:
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
                    raise WorkspaceError(
                        f"Invalid old_text='': expected a nonempty exact fragment from "
                        f"path={display_value(path)}. "
                        "Read the current file with workspace_read_file "
                        "and choose a unique fragment."
                    )
                with filesystem_errors(path, text_parameter="new_text"):
                    candidate = self.writable_path(path.relative, root=path.area)
                    text = read_text(candidate, path)
                    count = text.count(old_text)
                    if count == 0:
                        raise WorkspaceError(
                            f"Invalid old_text: no exact match in path={display_value(path)}; "
                            "the file was not changed. Read the current contents with "
                            "workspace_read_file and match the exact whitespace and line endings."
                        )
                    if count > 1 and not replace_all:
                        raise WorkspaceError(
                            f"Invalid old_text: found {count} occurrences "
                            f"in path={display_value(path)}; "
                            "the file was not changed. Use a larger unique old_text fragment, "
                            "or replace_all=true if every occurrence should change."
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
            return tool_result(result, render_text(result))

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
        ) -> Annotated[CallToolResult, PathOperationResult]:
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
                    raise WorkspaceError(
                        f"Invalid source={display_value(source)}, "
                        f"destination={display_value(destination)}: workspace_move requires "
                        "both paths within project/ or both within storage/. "
                        "Choose a destination in the source's managed root."
                    )
                with filesystem_errors(source, parameter="source"):
                    origin = self.writable_path(
                        source.relative,
                        root=source.area,
                        subtree=True,
                        parameter="source",
                    )
                    if not origin.exists():
                        raise WorkspaceError(
                            f"Invalid source={display_value(source)}: the path does not exist. "
                            "Use workspace_list or workspace_find_files to locate the source."
                        )
                    if origin.is_dir():
                        self.check_tree_links(origin)
                with filesystem_errors(destination, parameter="destination"):
                    target = self.writable_path(
                        destination.relative,
                        root=destination.area,
                        subtree=True,
                        parameter="destination",
                    )
                    if target.exists() or target.is_symlink():
                        raise WorkspaceError(
                            f"Invalid destination={display_value(destination)}: "
                            "the path already exists. Choose an unused destination; "
                            "workspace_move does not overwrite existing paths."
                        )
                    if origin.is_dir():
                        if target.is_relative_to(origin):
                            raise WorkspaceError(
                                f"Invalid destination={display_value(destination)}: it is inside "
                                f"source={display_value(source)}. "
                                "Choose a destination outside the source tree."
                            )
                    if not target.parent.is_dir():
                        raise WorkspaceError(
                            f"Invalid destination={display_value(destination)}: "
                            "its parent directory "
                            f"{display_value(self.workspace_path(target.parent))} does not exist "
                            "or is not a directory. "
                            "Use workspace_mkdir to create a missing parent, "
                            "or choose an existing destination directory."
                        )
                    try:
                        origin.rename(target)
                    except OSError as error:
                        raise WorkspaceError(
                            f"Cannot move source={display_value(source)} to "
                            f"destination={display_value(destination)}: {os_error_reason(error)}. "
                            "Inspect both paths with workspace_list and check their permissions "
                            "and whether another process has locked either path."
                        ) from error
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
            return tool_result(result, render_text(result))

        @apps.tool(
            resource_uri=self.RESULT_WIDGET.uri,
            icons=[self.EDIT_ICON.icon],
            annotations=modifying,
        )
        @serialized
        async def workspace_delete(
            path: WorkspacePath,
            ctx: Context,
        ) -> Annotated[CallToolResult, PathOperationResult]:
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
            return tool_result(result, render_text(result))

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
        ) -> Annotated[CallToolResult, PathOperationResult]:
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
            return tool_result(result, render_text(result))

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
            """Decode a managed resource path and translate invalid roots to resource errors."""
            try:
                # Directory completions end in '/', while WorkspacePath is canonical.
                area, separator, relative = value.partition("/")
                if area == "root":
                    raise ResourceNotFoundError(
                        f"Invalid path={display_value(value)}: external files have no workspace "
                        "mirror resources. Use workspace_read_file for an external UTF-8 file; "
                        "it requires operator approval."
                    )
                if not separator:
                    raise WorkspaceError(
                        f"Invalid path={display_value(value)}: resource paths must start with "
                        "project/ or storage/, for example 'project/src/main.cpp'."
                    )
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
                return self.render_markdown(DirectoryTree.model_validate(result.structured_content))
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
                return self.render_markdown(FilePaths.model_validate(result.structured_content))
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
                return self.render_markdown(FileInfo.model_validate(result.structured_content))
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
            max_matches: Annotated[int, Field(ge=1)] = 100,
        ) -> str:
            """Read text/regex matches and skipped files as Markdown; query is required."""
            try:
                result = await workspace_text_search(
                    ctx=ctx,
                    query=query,
                    path=resource_path(path),
                    regex=regex,
                    extensions=None if extensions is None else extensions.split(","),
                    case_sensitive=case_sensitive,
                    max_matches=max_matches,
                )
                return self.render_markdown(SearchResult.model_validate(result.structured_content))
            except (WorkspaceError, ToolError) as error:
                raise ResourceError(str(error)) from error

        async def workspace_completion(
            ref: ResourceTemplateReference,
            argument: CompletionArgument,
            context: CompletionContext | None,
        ) -> Completion | None:
            """Complete managed paths, filter options, and immutable result resource identifiers."""
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
