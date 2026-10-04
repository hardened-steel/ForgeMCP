"""Typed methods for lldb-dap."""

from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

from forgemcp.process.service import ProcessService

from ..spec import ToolInfo, ToolKind, ToolSpec


class Methods(TypedDict):
    """The typed operations supported by the bound lldb-dap executable."""


def create_spec(
    path: Path,
    processes: ProcessService,
    environment: Mapping[str, str] | None = None,
    inherit_environment: bool = True,
) -> ToolSpec:
    """Bind the resolved lldb-dap executable to process execution and environment settings."""
    path = path.resolve()
    methods: Methods = {}
    return ToolSpec(INFO.name, INFO.kind, path, methods)


INFO = ToolInfo(name='lldb-dap', kind=ToolKind.DEBUGGER, create_spec=create_spec)
