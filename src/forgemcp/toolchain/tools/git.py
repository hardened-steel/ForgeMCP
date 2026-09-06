"""Commands and parsers for git."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("--version",)


parse_version = version_parser("git version ([0-9][^\\s]*)")

SPEC = ToolSpec(
    name="git", kind=ToolKind.VERSION_CONTROL, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
