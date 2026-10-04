"""Typed methods for link."""

from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

from forgemcp.process.service import ProcessService

from ..spec import ToolInfo, ToolKind, ToolSpec


class Methods(TypedDict):
    """The typed operations supported by the bound link executable."""


def create_spec(
    path: Path,
    processes: ProcessService,
    environment: Mapping[str, str] | None = None,
    inherit_environment: bool = True,
) -> ToolSpec:
    """Bind the resolved link executable to process execution and environment settings."""
    path = path.resolve()
    methods: Methods = {}
    return ToolSpec(INFO.name, INFO.kind, path, methods)


INFO = ToolInfo(name='link', kind=ToolKind.LINKER, create_spec=create_spec)
