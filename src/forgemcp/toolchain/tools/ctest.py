"""Commands and parsers for ctest."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("--version",)


parse_version = version_parser("ctest version ([0-9][0-9A-Za-z.+~-]*)")

SPEC = ToolSpec(
    name="ctest", kind=ToolKind.TEST_RUNNER, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
