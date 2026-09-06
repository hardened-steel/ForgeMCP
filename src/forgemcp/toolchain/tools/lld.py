"""Commands and parsers for lld."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("-flavor", "gnu", "--version",)


parse_version = version_parser("LLD ([0-9][^\\s]*)")

SPEC = ToolSpec(
    name="lld", kind=ToolKind.LINKER, path=None, version=None,
    commands={"version": ToolCommand(version_arguments, parser="version")},
    parsers={"version": parse_version},
)
