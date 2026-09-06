"""Commands and parsers for ninja."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("--version",)


parse_version = version_parser("^([0-9]+\\.[0-9]+[^\\s]*)")

SPEC = ToolSpec(
    name="ninja", kind=ToolKind.BUILD_RUNNER, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
