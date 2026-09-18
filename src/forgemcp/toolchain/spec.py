"""Tool metadata, ready executable specs, and independent toolsets."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import NotRequired, ReadOnly, TypedDict

from forgemcp.process.service import ProcessService

from .errors import ToolCommandError


class ToolKind(StrEnum):
    BUILD_SYSTEM = "build_system"
    BUILD_RUNNER = "build_runner"
    TEST_RUNNER = "test_runner"
    COMPILER = "compiler"
    LINKER = "linker"
    LANGUAGE_SERVER = "language_server"
    VERSION_CONTROL = "version_control"
    DEBUGGER = "debugger"
    OTHER = "other"


class ToolMethods(TypedDict):
    """The subset of tool methods understood by toolchain."""

    version: ReadOnly[NotRequired[Callable[[], Awaitable[str]]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: ToolKind
    path: Path
    methods: ToolMethods


@dataclass(frozen=True)
class ToolInfo:
    """Discovery metadata and a factory for a ready executable spec."""

    name: str
    kind: ToolKind
    create_spec: Callable[[Path, ProcessService, Mapping[str, str] | None, bool], ToolSpec]


@dataclass(frozen=True)
class Toolset:
    id: str
    name: str
    tools: tuple[ToolSpec, ...]
    environment: Mapping[str, str] | None
    inherit_environment: bool

    def __post_init__(self) -> None:
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ToolCommandError("Toolset contains duplicate tool names.")
        object.__setattr__(self, "tools", tuple(sorted(self.tools, key=lambda tool: tool.name)))
        if self.environment is not None:
            object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))
