"""Workspace feature errors."""


class WorkspaceError(Exception):
    """Base class for expected workspace failures."""


class WorkspaceNotFoundError(WorkspaceError):
    """Raised when the configured workspace does not exist."""


class WorkspaceNotDirectoryError(WorkspaceError):
    """Raised when the configured workspace is not a directory."""


class UnsupportedExtensionError(WorkspaceError):
    """Raised when a file-list resource receives an unknown extension."""
