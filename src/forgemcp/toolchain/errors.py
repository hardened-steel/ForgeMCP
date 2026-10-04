"""Expected toolchain configuration, discovery, and execution failures."""


class ToolchainError(Exception):
    """Base class for expected toolchain failures."""


class ToolModuleError(ToolchainError):
    """A built-in tool module does not satisfy the INFO contract."""


class DuplicateToolSpecError(ToolModuleError):
    """Two built-in modules declare the same tool name."""


class UnknownToolError(ToolchainError):
    """Configuration names an unknown tool."""


class ToolsetNotFoundError(ToolchainError):
    """The requested toolset is not in the toolchain."""


class DuplicateToolsetError(ToolchainError):
    """Toolset names or IDs collide."""


class InvalidToolPathError(ToolchainError):
    """An explicitly configured tool path is not an executable file."""


class ToolCommandError(ToolchainError):
    """Tool arguments or process execution failed."""

    def __init__(self, message: str, *, process_id: int | None = None) -> None:
        """Retain a command failure explanation and optional process transcript identifier."""
        super().__init__(message)
        self.process_id = process_id


class ToolParserError(ToolchainError):
    """A parser failed for a particular tool command."""
