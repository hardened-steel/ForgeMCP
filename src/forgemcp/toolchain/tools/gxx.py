"""Commands and parsers for g++."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("-dumpfullversion", "-dumpversion",)


parse_version = version_parser("^([0-9]+\\.[0-9]+[^\\s]*)")

SPEC = ToolSpec(
    name="g++", kind=ToolKind.COMPILER, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
