"""Typed methods for link."""

from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

from forgemcp.process.service import ProcessService

from ..spec import ToolInfo, ToolKind, ToolSpec


class Methods(TypedDict):
    pass


def create_spec(
    path: Path,
    processes: ProcessService,
    environment: Mapping[str, str] | None = None,
    inherit_environment: bool = True,
) -> ToolSpec:
    path = path.resolve()
    methods: Methods = {}
    return ToolSpec(INFO.name, INFO.kind, path, methods)


INFO = ToolInfo(name='link', kind=ToolKind.LINKER, create_spec=create_spec)
