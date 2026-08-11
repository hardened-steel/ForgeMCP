"""Expected domain errors and their safe MCP-facing representation."""

from __future__ import annotations

from dataclasses import dataclass


class ForgeMCPError(Exception):
    """Base class for expected errors that may be shown to MCP clients."""

    code = "forge_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigurationError(ForgeMCPError):
    """Configuration is syntactically invalid or internally inconsistent."""

    code = "configuration_error"


class WorkspaceRootError(ConfigurationError):
    """The configured workspace root cannot be used."""

    code = "workspace_root_error"


class ServiceAlreadyRegisteredError(ForgeMCPError):
    """A service name is registered more than once."""

    code = "service_already_registered"


class ServiceNotFoundError(ForgeMCPError):
    """A requested service is not registered in the application container."""

    code = "service_not_found"


class LifecycleError(ForgeMCPError):
    """An application lifecycle transition is invalid."""

    code = "lifecycle_error"


@dataclass(frozen=True, slots=True)
class McpErrorResponse:
    """A stable, non-sensitive representation of an expected error."""

    code: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {"ok": False, "error": {"code": self.code, "message": self.message}}


def to_mcp_error_response(error: ForgeMCPError) -> McpErrorResponse:
    """Map an expected Core error to a structured MCP tool result."""
    return McpErrorResponse(code=error.code, message=error.message)
