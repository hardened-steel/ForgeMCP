"""Commands and parsers for clangd."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("--version",)


parse_version = version_parser("clangd version ([0-9][^\\s]*)")

SPEC = ToolSpec(
    name="clangd", kind=ToolKind.LANGUAGE_SERVER, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
