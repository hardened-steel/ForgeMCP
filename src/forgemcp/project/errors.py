"""Safe domain errors for Project Intelligence status aggregation."""

from __future__ import annotations

from forgemcp.core.errors import ForgeMCPError


class ProjectStatusError(ForgeMCPError):
    """Base class for safe project-status failures."""

    code = "project_status_error"


class DuplicateProjectStatusProviderError(ProjectStatusError):
    """Raised when a stable provider identifier is already registered."""

    code = "project_status_provider_duplicate"


class ProjectStatusRegistryClosedError(ProjectStatusError):
    """Raised when a snapshot is requested after registry shutdown began."""

    code = "project_status_unavailable"


class ProjectStatusRequestError(ProjectStatusError):
    """Raised when tool arguments do not match the no-input contract."""

    code = "project_status_invalid_request"
