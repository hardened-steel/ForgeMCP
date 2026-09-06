"""Commands and parsers for link."""

from ..spec import ToolCommand, ToolKind, ToolSpec, version_parser


def version_arguments() -> tuple[str, ...]:
    return ("/?",)


parse_version = version_parser("Linker Version ([0-9.]+)")

SPEC = ToolSpec(
    name="link", kind=ToolKind.LINKER, path=None, version=None,
    # LINK's help command uses 1100 for a normal help-only invocation.
    commands={"version": ToolCommand(version_arguments, parser="version", success_codes=frozenset({0, 1100}))},
    parsers={"version": parse_version},
)
