"""Workspace business logic and MCP registration."""

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
    PromptReference,
    ResourceTemplateReference,
    ToolAnnotations,
)
from pydantic import BaseModel, Field

from forgemcp.assets import IconFile, Widget
from forgemcp.completion import Complete

from .errors import (
    UnsupportedExtensionError,
    WorkspaceNotDirectoryError,
    WorkspaceNotFoundError,
)


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

    WIDGET = Widget("assets/workspace-overview.html")
    ICON = IconFile("icons/workspace.svg")
    FILES_URI = "forgemcp://workspace/files/{extension}"
    PROMPT = "inspect_cpp_workspace"
    SOURCE_EXTENSIONS = frozenset({"c", "cc", "cpp", "cxx"})
    HEADER_EXTENSIONS = frozenset({"h", "hh", "hpp", "hxx"})
    SUPPORTED_EXTENSIONS = tuple(sorted(SOURCE_EXTENSIONS | HEADER_EXTENSIONS))
    IGNORED_DIRECTORIES = frozenset(
        {".cache", ".git", ".idea", ".pytest_cache", ".venv", ".vscode", "build", "out"}
    )
    MAX_SCANNED_FILES = 10_000
    MAX_RESOURCE_FILES = 500

    def __init__(self, workspace_root: Path) -> None:
        if not workspace_root.exists():
            raise WorkspaceNotFoundError(f"Workspace does not exist: {workspace_root}")
        if not workspace_root.is_dir():
            raise WorkspaceNotDirectoryError(f"Workspace is not a directory: {workspace_root}")
        self.root = workspace_root.resolve()

    def overview(self) -> WorkspaceOverview:
        """Return a bounded C/C++ file summary without changing the workspace."""
        counts: Counter[str] = Counter()
        scanned = 0
        truncated = False

        for path in self.iter_files():
            scanned += 1
            if scanned > self.MAX_SCANNED_FILES:
                truncated = True
                break
            extension = path.suffix.removeprefix(".").lower()
            if extension in self.SUPPORTED_EXTENSIONS:
                counts[extension] += 1

        return WorkspaceOverview(
            name=self.root.name,
            source_files=sum(counts[extension] for extension in self.SOURCE_EXTENSIONS),
            header_files=sum(counts[extension] for extension in self.HEADER_EXTENSIONS),
            files_by_extension=dict(sorted(counts.items())),
            has_cmake_lists=(self.root / "CMakeLists.txt").is_file(),
            has_cmake_presets=(self.root / "CMakePresets.json").is_file(),
            scan_truncated=truncated,
        )

    def files(self, extension: str) -> tuple[list[str], bool]:
        """Return bounded workspace-relative paths for one supported extension."""
        normalized = extension.lower().removeprefix(".")
        if normalized not in self.SUPPORTED_EXTENSIONS:
            raise UnsupportedExtensionError(
                f"Unsupported extension {extension!r}; expected one of "
                f"{', '.join(self.SUPPORTED_EXTENSIONS)}."
            )

        matches: list[str] = []
        truncated = False
        for path in self.iter_files():
            if path.suffix.lower() != f".{normalized}":
                continue
            if len(matches) == self.MAX_RESOURCE_FILES:
                truncated = True
                break
            matches.append(path.relative_to(self.root).as_posix())
        matches.sort()
        return matches, truncated

    def files_resource(self, extension: str) -> str:
        """Return the JSON body for the workspace files resource."""
        try:
            paths, truncated = self.files(extension)
        except UnsupportedExtensionError as error:
            raise ResourceNotFoundError(str(error)) from error
        return json.dumps(
            {
                "extension": extension.lower().removeprefix("."),
                "files": paths,
                "truncated": truncated,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def inspection_prompt(self, focus: str) -> str:
        """Render the workspace inspection prompt."""
        return (
            "Inspect the configured C/C++ workspace. "
            f"Focus on {focus}. Call workspace_overview first, use workspace file "
            "resources when useful, and clearly separate observed facts from "
            "recommendations. Do not modify files unless the user asks."
        )

    def iter_files(self) -> Iterator[Path]:
        """Yield files from the bounded workspace traversal domain."""
        for directory, child_directories, filenames in os.walk(self.root, followlinks=False):
            child_directories[:] = [
                name for name in child_directories if name not in self.IGNORED_DIRECTORIES
            ]
            directory_path = Path(directory)
            for filename in filenames:
                yield directory_path / filename

    def matching_completion(self, values: tuple[str, ...], prefix: str) -> Completion:
        """Return case-insensitive prefix matches."""
        normalized_prefix = prefix.lower()
        return Completion(values=[value for value in values if value.startswith(normalized_prefix)])

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        """Register all workspace MCP entrypoints."""
        icon = self.ICON.icon

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[icon],
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        async def workspace_overview(ctx: Context) -> WorkspaceOverview:
            """Summarize the configured C/C++ workspace without modifying it."""
            await ctx.report_progress(1, total=3, message="Validating workspace")
            await ctx.report_progress(2, total=3, message="Scanning C/C++ files")
            result = self.overview()
            await ctx.report_progress(3, total=3, message="Workspace overview ready")
            return result

        apps.add_html_resource(self.WIDGET.uri, self.WIDGET.content)

        @mcp.resource(self.FILES_URI, mime_type="application/json", icons=[icon])
        def workspace_files(extension: str) -> str:
            """List workspace-relative C/C++ files with the selected extension."""
            return self.files_resource(extension)

        @mcp.prompt(icons=[icon])
        def inspect_cpp_workspace(focus: str = "overview") -> str:
            """Start a focused inspection of the configured C/C++ workspace."""
            return self.inspection_prompt(focus)

        async def workspace_completion(
            ref: PromptReference | ResourceTemplateReference,
            argument: CompletionArgument,
            context: CompletionContext | None,
        ) -> Completion | None:
            del context
            if (
                isinstance(ref, ResourceTemplateReference)
                and ref.uri == self.FILES_URI
                and argument.name == "extension"
            ):
                return self.matching_completion(self.SUPPORTED_EXTENSIONS, argument.value)
            if (
                isinstance(ref, PromptReference)
                and ref.name == self.PROMPT
                and argument.name == "focus"
            ):
                return self.matching_completion(
                    ("overview", "build", "tests", "toolchain", "code-quality"),
                    argument.value,
                )
            return None

        complete.add_completion(workspace_completion)
