"""Visual Studio debug adapter (no portable version command)."""

from ..spec import ToolKind, ToolSpec

SPEC = ToolSpec(
    name="cppvsdbg", kind=ToolKind.DEBUGGER, path=None, version=None,
    commands={}, parsers={},
)
