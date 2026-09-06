"""Commands and parsers for cl."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("/?",)


parse_version = version_parser(r"Microsoft \(R\).*?C/C\+\+.*?([0-9]+\.[0-9]+\.[0-9]+)")

SPEC = ToolSpec(
    name="cl", kind=ToolKind.COMPILER, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
