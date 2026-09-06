"""Commands and parsers for make."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("--version",)


parse_version = version_parser("GNU Make ([0-9][^\\s]*)")

SPEC = ToolSpec(
    name="make", kind=ToolKind.BUILD_RUNNER, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
