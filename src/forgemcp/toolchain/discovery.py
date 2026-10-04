"""Assemble independent toolsets with ready executable specs."""

from collections.abc import Sequence

from forgemcp.process.service import ProcessService

from .errors import DuplicateToolsetError
from .loader import load_tools
from .providers import system, user, visual_studio
from .spec import Toolset


async def discover(
    processes: ProcessService,
    definitions: Sequence[Sequence[str]] = (),
) -> tuple[Toolset, ...]:
    specs = load_tools()
    # Validate all explicit configuration before launching discovery processes.
    users = user.parse_toolsets(definitions, specs)
    toolsets = [system.discover(specs, processes)]
    toolsets.extend(await visual_studio.discover(specs, processes))
    toolsets.extend(user.discover(users, specs, processes))
    if len({toolset.id for toolset in toolsets}) != len(toolsets):
        raise DuplicateToolsetError("Discovery produced duplicate toolset IDs.")
    return tuple(sorted(toolsets, key=lambda item: item.id))
