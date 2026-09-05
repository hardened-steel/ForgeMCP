"""Business logic and MCP registration for workspace inspection."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.types import (
    Completion,
    CompletionArgument,
    CompletionContext,
    Icon,
    PromptReference,
    ResourceTemplateReference,
    ToolAnnotations,
)
from pydantic import BaseModel, Field

from .errors import (
    UnsupportedExtensionError,
    WorkspaceNotDirectoryError,
    WorkspaceNotFoundError,
)

WORKSPACE_APP_URI = "ui://forgemcp/workspace-overview.html"
WORKSPACE_FILES_URI = "forgemcp://workspace/files/{extension}"
INSPECT_WORKSPACE_PROMPT = "inspect_cpp_workspace"

WORKSPACE_ICON = Icon(
    src=(
        "data:image/svg+xml;base64,"
        "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+"
        "PHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0IiByeD0iNSIgZmlsbD0iIzViNWJkNiIvPjxwYXRoIGQ9"
        "Ik03IDZoMTB2Mkg3em0wIDVoN3YySDd6bTAgNWgxMHYySDd6IiBmaWxsPSJ3aGl0ZSIvPjwvc3ZnPg=="
    ),
    mime_type="image/svg+xml",
    sizes=["any"],
)

_SOURCE_EXTENSIONS = frozenset({"c", "cc", "cpp", "cxx"})
_HEADER_EXTENSIONS = frozenset({"h", "hh", "hpp", "hxx"})
_SUPPORTED_EXTENSIONS = tuple(sorted(_SOURCE_EXTENSIONS | _HEADER_EXTENSIONS))
_IGNORED_DIRECTORIES = frozenset(
    {".cache", ".git", ".idea", ".pytest_cache", ".venv", ".vscode", "build", "out"}
)
_MAX_SCANNED_FILES = 10_000
_MAX_RESOURCE_FILES = 500


class WorkspaceOverview(BaseModel):
    """A bounded summary of the configured C/C++ workspace."""

    name: str = Field(description="Workspace directory name.")
    source_files: int = Field(description="Number of discovered C/C++ source files.")
    header_files: int = Field(description="Number of discovered C/C++ header files.")
    files_by_extension: dict[str, int] = Field(
        description="Counts keyed by extension without a dot."
    )
    has_cmake_lists: bool = Field(
        description="Whether the workspace root contains CMakeLists.txt."
    )
    has_cmake_presets: bool = Field(
        description="Whether the workspace root contains CMakePresets.json."
    )
    scan_truncated: bool = Field(
        description="Whether the bounded scan stopped before visiting every file."
    )


class WorkspaceService:
    """Inspect one workspace and expose its initial MCP surface."""

    def __init__(self, workspace_root: Path) -> None:
        if not workspace_root.exists():
            raise WorkspaceNotFoundError(f"Workspace does not exist: {workspace_root}")
        if not workspace_root.is_dir():
            raise WorkspaceNotDirectoryError(f"Workspace is not a directory: {workspace_root}")
        self._root = workspace_root.resolve()

    def overview(self) -> WorkspaceOverview:
        """Return a bounded C/C++ file summary without changing the workspace."""
        counts: Counter[str] = Counter()
        scanned = 0
        truncated = False

        for path in self._iter_files():
            scanned += 1
            if scanned > _MAX_SCANNED_FILES:
                truncated = True
                break
            extension = path.suffix.removeprefix(".").lower()
            if extension in _SUPPORTED_EXTENSIONS:
                counts[extension] += 1

        return WorkspaceOverview(
            name=self._root.name,
            source_files=sum(counts[extension] for extension in _SOURCE_EXTENSIONS),
            header_files=sum(counts[extension] for extension in _HEADER_EXTENSIONS),
            files_by_extension=dict(sorted(counts.items())),
            has_cmake_lists=(self._root / "CMakeLists.txt").is_file(),
            has_cmake_presets=(self._root / "CMakePresets.json").is_file(),
            scan_truncated=truncated,
        )

    def files(self, extension: str) -> tuple[list[str], bool]:
        """Return bounded workspace-relative paths for one supported extension."""
        normalized = extension.lower().removeprefix(".")
        if normalized not in _SUPPORTED_EXTENSIONS:
            raise UnsupportedExtensionError(
                f"Unsupported extension {extension!r}; expected one of "
                f"{', '.join(_SUPPORTED_EXTENSIONS)}."
            )

        matches: list[str] = []
        truncated = False
        for path in self._iter_files():
            if path.suffix.lower() != f".{normalized}":
                continue
            if len(matches) == _MAX_RESOURCE_FILES:
                truncated = True
                break
            matches.append(path.relative_to(self._root).as_posix())
        matches.sort()
        return matches, truncated

    async def workspace_overview(self, ctx: Context) -> WorkspaceOverview:
        """Summarize the configured C/C++ workspace without modifying it."""
        await ctx.report_progress(1, total=3, message="Validating workspace")
        await ctx.report_progress(2, total=3, message="Scanning C/C++ files")
        result = self.overview()
        await ctx.report_progress(3, total=3, message="Workspace overview ready")
        return result

    def workspace_files(self, extension: str) -> str:
        """List workspace-relative C/C++ files with the selected extension."""
        try:
            files, truncated = self.files(extension)
        except UnsupportedExtensionError as error:
            raise ResourceNotFoundError(str(error)) from error
        return json.dumps(
            {
                "extension": extension.lower().removeprefix("."),
                "files": files,
                "truncated": truncated,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def inspect_cpp_workspace(self, focus: str = "overview") -> str:
        """Start a focused inspection of the configured C/C++ workspace."""
        return (
            "Inspect the configured C/C++ workspace. "
            f"Focus on {focus}. Call workspace_overview first, use workspace file "
            "resources when useful, and clearly separate observed facts from "
            "recommendations. Do not modify files unless the user asks."
        )

    async def complete(
        self,
        ref: PromptReference | ResourceTemplateReference,
        argument: CompletionArgument,
        context: CompletionContext | None,
    ) -> Completion | None:
        """Complete this feature's resource-template and prompt arguments."""
        del context
        if (
            isinstance(ref, ResourceTemplateReference)
            and ref.uri == WORKSPACE_FILES_URI
            and argument.name == "extension"
        ):
            return self._matching_completion(_SUPPORTED_EXTENSIONS, argument.value)
        if (
            isinstance(ref, PromptReference)
            and ref.name == INSPECT_WORKSPACE_PROMPT
            and argument.name == "focus"
        ):
            return self._matching_completion(
                ("overview", "build", "tests", "toolchain", "code-quality"), argument.value
            )
        return None

    def register_apps(self, apps: Apps) -> None:
        """Register UI-bound tools before the MCP server consumes the Apps extension."""
        apps.tool(
            resource_uri=WORKSPACE_APP_URI,
            name="workspace_overview",
            title="Workspace overview",
            description="Summarize the configured C/C++ workspace without modifying it.",
            annotations=ToolAnnotations(
                title="Workspace overview",
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
            icons=[WORKSPACE_ICON],
            structured_output=True,
        )(self.workspace_overview)
        widget_path = Path(__file__).parent / "assets" / "workspace-overview.html"
        apps.add_html_resource(
            WORKSPACE_APP_URI,
            widget_path.read_text(encoding="utf-8"),
            name="workspace_overview_widget",
            title="Workspace overview",
            description="Interactive summary for the workspace_overview tool.",
            prefers_border=True,
        )

    def register(self, mcp: MCPServer) -> None:
        """Register this feature's resources and prompts."""
        mcp.resource(
            WORKSPACE_FILES_URI,
            name="workspace_files",
            title="Workspace C/C++ files",
            description="Bounded list of workspace-relative files for one C/C++ extension.",
            mime_type="application/json",
            icons=[WORKSPACE_ICON],
        )(self.workspace_files)
        mcp.prompt(
            name=INSPECT_WORKSPACE_PROMPT,
            title="Inspect C/C++ workspace",
            description="Start a read-only, focused inspection of the configured workspace.",
            icons=[WORKSPACE_ICON],
        )(self.inspect_cpp_workspace)

    def _iter_files(self) -> Iterator[Path]:
        for directory, child_directories, filenames in os.walk(self._root, followlinks=False):
            child_directories[:] = [
                name for name in child_directories if name not in _IGNORED_DIRECTORIES
            ]
            directory_path = Path(directory)
            for filename in filenames:
                yield directory_path / filename

    @staticmethod
    def _matching_completion(values: tuple[str, ...], prefix: str) -> Completion:
        normalized_prefix = prefix.lower()
        return Completion(values=[value for value in values if value.startswith(normalized_prefix)])
