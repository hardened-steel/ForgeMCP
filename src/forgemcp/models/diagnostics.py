"""Tool-independent diagnostic models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from forgemcp.models._base import ForgeModel
from forgemcp.models.locations import Location


class Severity(StrEnum):
    """User-facing importance levels for a diagnostic."""

    ERROR = "error"
    WARNING = "warning"
    INFORMATION = "information"
    HINT = "hint"


class Diagnostic(ForgeModel):
    """A bounded, actionable finding associated with a source location."""

    message: str = Field(
        min_length=1,
        max_length=16_384,
        description="Human-readable diagnostic text without an assumed transport format.",
    )
    severity: Severity = Field(description="Importance level of the finding.")
    location: Location = Field(description="Primary source location for the finding.")
    code: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Optional stable identifier supplied by the diagnostic producer.",
    )
    source: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        description="Optional producer name, such as a compiler or static analyzer.",
    )
