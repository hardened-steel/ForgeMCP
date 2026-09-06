"""Expected toolchain configuration, discovery, and execution failures."""


class ToolchainError(Exception):
    """Base class for expected toolchain failures."""


class ToolModuleError(ToolchainError):
    """A built-in tool module does not satisfy the SPEC contract."""


class DuplicateToolSpecError(ToolModuleError):
    """Two built-in modules declare the same tool name."""


class UnknownToolError(ToolchainError):
    """Configuration names an unknown tool."""


class ToolsetNotFoundError(ToolchainError):
    """The requested toolset is not in the discovery cache."""


class DuplicateToolsetError(ToolchainError):
    """Toolset names or IDs collide."""


class InvalidToolPathError(ToolchainError):
    """An explicitly configured tool path is not an executable file."""


class ToolCommandError(ToolchainError):
    """Command binding, arguments, or process execution failed."""


class ToolParserError(ToolchainError):
    """A parser failed for a particular tool command."""
