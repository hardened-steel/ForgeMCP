"""Validate explicit CLI toolsets without discovery or fallback."""

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping

from forgemcp.process.service import ProcessService

from ..errors import (
    DuplicateToolsetError, InvalidToolPathError, ToolCommandError, UnknownToolError,
)
from ..spec import ToolInfo, Toolset


@dataclass(frozen=True)
class UserToolset:
    name: str
    paths: Mapping[str, Path]

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", MappingProxyType(dict(self.paths)))


def parse_toolsets(
    values: Sequence[Sequence[str]], specs: tuple[ToolInfo, ...],
) -> tuple[UserToolset, ...]:
    known = {spec.name for spec in specs}
    names: set[str] = set()
    result = []
    for group in values:
        if len(group) < 2 or not group[0].strip() or "=" in group[0]:
            raise ToolCommandError("--toolset requires NAME TOOL=PATH [TOOL=PATH ...].")
        name = group[0]
        if name in names:
            raise DuplicateToolsetError(f"Duplicate user toolset {name!r}.")
        names.add(name)
        paths = {}
        for assignment in group[1:]:
            tool, separator, value = assignment.partition("=")
            if tool not in known:
                raise UnknownToolError(f"Unknown tool {tool!r} in toolset {name!r}.")
            if tool in paths:
                raise ToolCommandError(f"Duplicate tool {tool!r} in toolset {name!r}.")
            try:
                path = Path(value).expanduser().resolve(strict=True)
                valid = (bool(separator and value) and path.is_file()
                         and (os.name == "nt" or os.access(path, os.X_OK)))
                if os.name == "nt":
                    suffixes = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").lower().split(";")
                    valid = valid and path.suffix.lower() in suffixes
            except (OSError, ValueError, RuntimeError):
                valid = False
            if not valid:
                raise InvalidToolPathError(f"Invalid path for {tool!r} in toolset {name!r}.")
            paths[tool] = path
        result.append(UserToolset(name, paths))
    return tuple(result)


def discover(
    definitions: tuple[UserToolset, ...], specs: tuple[ToolInfo, ...], processes: ProcessService,
) -> tuple[Toolset, ...]:
    by_name = {spec.name: spec for spec in specs}
    result = []
    for definition in definitions:
        tools = tuple(by_name[name].create_spec(path, processes, None, True)
                      for name, path in definition.paths.items())
        identifier = "user-" + hashlib.sha256(definition.name.encode("utf-8")).hexdigest()
        result.append(Toolset(identifier, definition.name, tools, None, True))
    return tuple(result)
