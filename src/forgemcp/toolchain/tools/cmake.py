"""Commands and parsers for cmake."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("--version",)


parse_version = version_parser("cmake version ([0-9][0-9A-Za-z.+~-]*)")

SPEC = ToolSpec(
    name="cmake", kind=ToolKind.BUILD_SYSTEM, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
