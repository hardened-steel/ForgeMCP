"""Commands and parsers for lld-link."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("--version",)


parse_version = version_parser("LLD ([0-9][^\\s]*)")

SPEC = ToolSpec(
    name="lld-link", kind=ToolKind.LINKER, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
