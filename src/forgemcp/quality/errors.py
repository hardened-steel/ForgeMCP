"""Safe domain errors for the Quality feature."""

from forgemcp.core.errors import ForgeMCPError


class QualityError(ForgeMCPError):
    """Base class for expected quality-tool failures."""

    code = "quality_error"


class QualityRequestError(QualityError):
    """A published Quality tool request is invalid or outside its bounded surface."""

    code = "quality_request_error"


class QualityToolUnavailableError(QualityError):
    """A requested fixed local executable was not qualified."""

    code = "quality_tool_unavailable"


class QualityToolExecutionError(QualityError):
    """A fixed quality executable could not produce a safe structured result."""

    code = "quality_tool_execution_error"


class QualityFormatConflictError(QualityError):
    """Snapshot CAS rejected a format batch before any target changed."""

    code = "quality_format_conflict"
