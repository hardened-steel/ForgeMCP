"""Immutable discovery cache, consumer API, and read-only MCP inspection."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import (
    Completion, CompletionArgument, CompletionContext, PromptReference,
    ResourceTemplateReference, ToolAnnotations,
)
from pydantic import BaseModel

from forgemcp.assets import IconFile, Widget
from forgemcp.completion import Complete
from forgemcp.process.service import ProcessService

from . import discovery
from .errors import ToolsetNotFoundError
from .spec import ToolKind, Toolset, ToolSpec


class ToolsetSummary(BaseModel):
    id: str
    name: str
    tools: list[str]


class ToolInfo(BaseModel):
    name: str
    kind: ToolKind
    path: str
    version: str | None


class ToolsetDetails(BaseModel):
    id: str
    name: str
    tools: list[ToolInfo]


def markdown(value: str) -> str:
    """Escape untrusted names and paths used in markdown table cells."""
    for character in ("\\", "`", "*", "_", "[", "]", "<", ">", "|", "#"):
        value = value.replace(character, "\\" + character)
    return value.replace("\r", " ").replace("\n", " ")


class ToolchainService:
    """Inspect cached independent toolsets, then pass an explicit toolset ID to consumers.

    Toolsets may be incomplete. No current or preferred toolset is selected. Absolute
    executable paths and versions are public; toolset environments remain internal.
    """

    WIDGET = Widget("assets/toolsets.html")
    ICON = IconFile("icons/toolchain.svg")
    LIST_URI = "forgemcp://toolsets"
    DETAILS_URI = "forgemcp://toolsets/{toolset_id}"

    def __init__(
        self, processes: ProcessService, definitions: Sequence[Sequence[str]] = (),
    ) -> None:
        self.processes = processes
        self.definitions = tuple(tuple(group) for group in definitions)
        self.cache: tuple[Toolset, ...] = ()
        self.initialized = False
        self.discovery_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Discover once and publish the complete immutable snapshot atomically."""
        async with self.discovery_lock:
            if not self.initialized:
                toolsets = await discovery.discover(self.processes, self.definitions)
                self.cache = toolsets
                self.initialized = True

    def list_toolsets(self) -> tuple[ToolsetSummary, ...]:
        return tuple(ToolsetSummary(id=item.id, name=item.name,
                                    tools=[tool.name for tool in item.tools]) for item in self.cache)

    def resolve_toolset(self, toolset_id: str) -> Toolset:
        for toolset in self.cache:
            if toolset.id == toolset_id:
                return toolset
        raise ToolsetNotFoundError(f"Unknown toolset ID {toolset_id!r}.")

    def get_toolset(self, toolset_id: str) -> ToolsetDetails:
        toolset = self.resolve_toolset(toolset_id)
        return ToolsetDetails(id=toolset.id, name=toolset.name, tools=[
            ToolInfo(name=tool.name, kind=tool.kind, path=str(tool.path), version=tool.version)
            for tool in toolset.tools
        ])

    def get_tool(self, toolset_id: str, tool_name: str) -> ToolSpec | None:
        return next((tool for tool in self.resolve_toolset(toolset_id).tools
                     if tool.name == tool_name), None)

    def summaries_markdown(self) -> str:
        lines = ["# Toolsets", "", "| ID | Name | Tools |", "| --- | --- | --- |"]
        for item in self.list_toolsets():
            lines.append(f"| {markdown(item.id)} | {markdown(item.name)} | "
                         f"{markdown(', '.join(item.tools))} |")
        return "\n".join(lines)

    def details_markdown(self, toolset_id: str) -> str:
        item = self.get_toolset(toolset_id)
        lines = [f"# {markdown(item.name)}", "", f"ID: {markdown(item.id)}", "",
                 "| Tool | Kind | Absolute path | Version |", "| --- | --- | --- | --- |"]
        for tool in item.tools:
            lines.append("| " + " | ".join(markdown(value) for value in (
                tool.name, tool.kind.value, tool.path, tool.version or "Unknown",
            )) + " |")
        return "\n".join(lines)

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        icon = self.ICON.icon
        annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                      idempotent_hint=True, open_world_hint=False)

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=annotations)
        async def toolsets_list(ctx: Context) -> list[ToolsetSummary]:
            """List all cached toolsets and the tools available in each."""
            await ctx.report_progress(1, total=2, message="Reading cached toolsets")
            result = list(self.list_toolsets())
            await ctx.report_progress(2, total=2, message="Toolsets ready")
            return result

        @apps.tool(resource_uri=self.WIDGET.uri, icons=[icon], annotations=annotations)
        async def toolset_get(toolset_id: str, ctx: Context) -> ToolsetDetails:
            """Read cached tool paths, kinds, and versions for an explicit toolset ID."""
            await ctx.report_progress(1, total=2, message="Reading cached toolset")
            try:
                result = self.get_toolset(toolset_id)
            except ToolsetNotFoundError as error:
                raise ToolError(str(error)) from error
            await ctx.report_progress(2, total=2, message="Toolset ready")
            return result

        apps.add_html_resource(self.WIDGET.uri, self.WIDGET.content)

        @mcp.resource(self.LIST_URI, mime_type="text/markdown", icons=[icon])
        def toolsets() -> str:
            """Read a markdown summary of all cached toolsets."""
            return self.summaries_markdown()

        @mcp.resource(self.DETAILS_URI, mime_type="text/markdown", icons=[icon])
        def toolset_details(toolset_id: str) -> str:
            """Read the cached executable paths and versions of one toolset."""
            try:
                return self.details_markdown(toolset_id)
            except ToolsetNotFoundError as error:
                raise ResourceNotFoundError(str(error)) from error

        async def toolset_completion(
            ref: PromptReference | ResourceTemplateReference,
            argument: CompletionArgument, context: CompletionContext | None,
        ) -> Completion | None:
            if (not isinstance(ref, ResourceTemplateReference) or ref.uri != self.DETAILS_URI
                    or argument.name != "toolset_id"):
                return None
            values = [item.id for item in self.cache if item.id.startswith(argument.value)]
            return Completion(values=values[:100], total=len(values), has_more=len(values) > 100)

        complete.add_completion(toolset_completion)
