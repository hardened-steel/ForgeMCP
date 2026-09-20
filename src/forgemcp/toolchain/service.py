"""Toolset containers, consumer API, and read-only MCP inspection."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import (
    Completion,
    CompletionArgument,
    CompletionContext,
    PromptReference,
    ResourceTemplateReference,
    ToolAnnotations,
)
from pydantic import BaseModel

from forgemcp import markdown
from forgemcp.assets import IconFile, Widget
from forgemcp.completion import Complete
from forgemcp.progress import progress
from forgemcp.process.service import ProcessService

from . import discovery
from .errors import ToolCommandError, ToolParserError, ToolsetNotFoundError
from .spec import ToolKind, Toolset, ToolSpec


class ToolsetSummary(BaseModel):
    id: str
    name: str
    tools: list[str]


class ToolDetails(BaseModel):
    name: str
    kind: ToolKind
    path: str
    version: str | None


class ToolsetDetails(BaseModel):
    id: str
    name: str
    tools: list[ToolDetails]


class ToolchainService:
    """Inspect independent toolsets, then pass an explicit toolset ID to consumers.

    Toolsets may be incomplete. No current or preferred toolset is selected. Absolute
    executable paths and versions are public; toolset environments remain internal.
    """

    WIDGET = Widget("assets/toolsets.html")
    ICON = IconFile("icons/toolchain.svg")
    LIST_URI = "forgemcp://toolsets"
    DETAILS_URI = "forgemcp://toolsets/{toolset_id}"

    def __init__(
        self,
        processes: ProcessService,
        definitions: Sequence[Sequence[str]] = (),
        *,
        progress_interval: float = 1.0,
    ) -> None:
        self.progress_interval = progress_interval
        self.processes = processes
        self.definitions = tuple(tuple(group) for group in definitions)
        self.toolsets: tuple[Toolset, ...] = ()
        self.initialized = False
        self.discovery_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Discover once and publish the complete collection atomically."""
        async with self.discovery_lock:
            if not self.initialized:
                toolsets = await discovery.discover(self.processes, self.definitions)
                self.toolsets = toolsets
                self.initialized = True

    def list_toolsets(self) -> tuple[Toolset, ...]:
        return self.toolsets

    def get_toolset(self, toolset_id: str) -> Toolset:
        for toolset in self.toolsets:
            if toolset.id == toolset_id:
                return toolset
        raise ToolsetNotFoundError(f"Unknown toolset ID {toolset_id!r}.")

    def get_tool(self, toolset_id: str, tool_name: str) -> ToolSpec | None:
        return next(
            (
                tool
                for tool in self.get_toolset(toolset_id).tools
                if tool.name == tool_name
            ),
            None,
        )

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        icon = self.ICON.icon
        annotations = ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        )

        async def read_tool(tool: ToolSpec) -> ToolDetails:
            # Versions belong only to the current MCP response, never the spec.
            version = None
            if "version" in tool.methods:
                try:
                    version = await tool.methods["version"]()
                except (ToolCommandError, ToolParserError):
                    pass
            return ToolDetails(
                name=tool.name,
                kind=tool.kind,
                path=str(tool.path),
                version=version,
            )

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=annotations)
        async def toolsets_list(ctx: Context) -> list[ToolsetSummary]:
            """List all discovered toolsets and the tools available in each."""
            report_progress = progress(ctx, interval=self.progress_interval)
            await report_progress(0, total=1, message="Reading toolsets")
            result = [
                ToolsetSummary(
                    id=item.id,
                    name=item.name,
                    tools=[tool.name for tool in item.tools],
                )
                for item in self.list_toolsets()
            ]
            await report_progress(1, total=1, message="Toolsets ready")
            return result

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=annotations)
        async def toolset_get(toolset_id: str, ctx: Context) -> ToolsetDetails:
            """Read tool paths and kinds, querying available versions for this request."""
            report_progress = progress(ctx, interval=self.progress_interval)
            try:
                toolset = self.get_toolset(toolset_id)
            except ToolsetNotFoundError as error:
                raise ToolError(str(error)) from error
            total = len(toolset.tools)
            await report_progress(0, total=total, message="Reading tool versions")
            details = []
            for index, tool in enumerate(toolset.tools, start=1):
                details.append(await read_tool(tool))
                await report_progress(
                    index,
                    total=total,
                    message=f"Read {tool.name}",
                )
            return ToolsetDetails(id=toolset.id, name=toolset.name, tools=details)

        apps.add_html_resource(self.WIDGET.uri, self.WIDGET.content)

        @mcp.resource(self.LIST_URI, mime_type="text/markdown", icons=[icon])
        def toolsets() -> str:
            """Read a markdown summary of all discovered toolsets."""
            table = markdown.Table(["ID", "Name", "Tools"])
            for item in self.list_toolsets():
                table.add(
                    [item.id, item.name, ", ".join(tool.name for tool in item.tools)]
                )
            return markdown.Document([markdown.Heading("Toolsets"), table]).render()

        @mcp.resource(self.DETAILS_URI, mime_type="text/markdown", icons=[icon])
        async def toolset_details(toolset_id: str) -> str:
            """Read executable paths and query available versions of one toolset."""
            try:
                toolset = self.get_toolset(toolset_id)
            except ToolsetNotFoundError as error:
                raise ResourceNotFoundError(str(error)) from error
            table = markdown.Table(["Tool", "Kind", "Absolute path", "Version"])
            for tool in toolset.tools:
                detail = await read_tool(tool)
                table.add(
                    [
                        detail.name,
                        detail.kind.value,
                        detail.path,
                        detail.version or "Unknown",
                    ]
                )
            return markdown.Document(
                [
                    markdown.Heading(toolset.name),
                    markdown.Paragraph(f"ID: {toolset.id}"),
                    table,
                ]
            ).render()

        async def toolset_completion(
            ref: PromptReference | ResourceTemplateReference,
            argument: CompletionArgument,
            context: CompletionContext | None,
        ) -> Completion | None:
            if (
                not isinstance(ref, ResourceTemplateReference)
                or ref.uri != self.DETAILS_URI
                or argument.name != "toolset_id"
            ):
                return None
            values = [
                item.id for item in self.toolsets if item.id.startswith(argument.value)
            ]
            return Completion(
                values=values[:100],
                total=len(values),
                has_more=len(values) > 100,
            )

        complete.add_completion(toolset_completion)
