"""Commands and parsers for lldb-dap."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


SPEC = ToolSpec(
    name="lldb-dap", kind=ToolKind.DEBUGGER, path=None, version=None,
    commands={},
    parsers={},
)
