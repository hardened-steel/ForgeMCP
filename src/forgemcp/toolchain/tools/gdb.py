"""Commands and parsers for gdb."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("--version",)


parse_version = version_parser(r"GNU gdb(?: \([^\r\n]*\))? ([0-9]+\.[0-9]+[^\s]*)")

SPEC = ToolSpec(
    name="gdb", kind=ToolKind.DEBUGGER, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
