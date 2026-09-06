"""Assemble independent toolsets and probe their bound version commands once."""

from collections.abc import Sequence
from dataclasses import replace

from forgemcp.process.service import ProcessService

from .errors import DuplicateToolsetError, ToolCommandError, ToolParserError
from .loader import load_tools
from .providers import system, user, visual_studio
from .spec import Toolset


async def discover(
    processes: ProcessService, definitions: Sequence[Sequence[str]] = (),
) -> tuple[Toolset, ...]:
    specs = load_tools()
    # Validate all explicit configuration before launching discovery processes.
    users = user.parse_toolsets(definitions, specs)
    toolsets = [system.discover(specs, processes)]
    toolsets.extend(await visual_studio.discover(specs, processes))
    toolsets.extend(user.discover(users, specs, processes))
    if len({toolset.id for toolset in toolsets}) != len(toolsets):
        raise DuplicateToolsetError("Discovery produced duplicate toolset IDs.")
    result = []
    for toolset in sorted(toolsets, key=lambda item: item.id):
        tools = []
        for tool in toolset.tools:
            version = None
            if "version" in tool.commands:
                try:
                    parsed = await tool.commands["version"]()
                    if isinstance(parsed, str):
                        version = parsed
                except (ToolCommandError, ToolParserError):
                    pass
            tools.append(replace(tool, version=version))
        result.append(replace(toolset, tools=tuple(tools)))
    return tuple(result)
