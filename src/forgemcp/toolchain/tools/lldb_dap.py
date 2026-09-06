"""Commands and parsers for lldb-dap."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("--version",)


parse_version = version_parser("(?:lldb|lldb-dap) version ([0-9][^\\s]*)")

SPEC = ToolSpec(
    name="lldb-dap", kind=ToolKind.DEBUGGER, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
