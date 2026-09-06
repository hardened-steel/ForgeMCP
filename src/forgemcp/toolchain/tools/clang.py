"""Commands and parsers for clang."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("--version",)


parse_version = version_parser("clang version ([0-9][^\\s]*)")

SPEC = ToolSpec(
    name="clang", kind=ToolKind.COMPILER, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
