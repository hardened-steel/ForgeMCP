"""Immutable transport-neutral models for Project Intelligence Phase 1."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator

from forgemcp.models._base import ForgeModel, normalize_utc


MAX_COMPONENTS = 64
MAX_CAPABILITIES = 128
MAX_FACTS = 32
MAX_WARNINGS = 32
MAX_STATUS_JSON_BYTES = 100_000
MIN_FACT_INTEGER = -(1 << 63)
MAX_FACT_INTEGER = (1 << 63) - 1

Identifier = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$"),
]
BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=512)]
WarningCode = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.:-]*$"),
]
FactValue = str | int | bool


class ProjectHealth(StrEnum):
    """Overall ForgeMCP service health, independent from operation results."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


class ProjectActivity(StrEnum):
    """Current aggregate project activity."""

    IDLE = "idle"
    BUSY = "busy"
    PAUSED = "paused"


class ComponentState(StrEnum):
    """Normalized cached lifecycle/availability state for one component."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    IDLE = "idle"
    STARTING = "starting"
    ACTIVE = "active"
    PAUSED = "paused"
    DEGRADED = "degraded"
    FAILED = "failed"
    STOPPED = "stopped"


class StatusFact(ForgeModel):
    """One bounded scalar fact; arbitrary JSON payloads are intentionally absent."""

    name: Identifier = Field(description="Stable fact identifier.")
    value: FactValue = Field(description="Bounded scalar string, integer, or boolean value.")
    unit: Annotated[str, StringConstraints(min_length=1, max_length=32)] | None = Field(
        default=None, description="Optional bounded unit label."
    )
    description: BoundedText | None = Field(
        default=None, description="Optional safe bounded explanation without raw output."
    )

    @field_validator("value")
    @classmethod
    def bound_string_value(cls, value: FactValue) -> FactValue:
        if isinstance(value, str) and (not value or len(value) > 256):
            raise ValueError("String fact values must contain from one through 256 characters.")
        if isinstance(value, int) and not isinstance(value, bool) and not MIN_FACT_INTEGER <= value <= MAX_FACT_INTEGER:
            raise ValueError("Integer fact values must fit the signed 64-bit status bound.")
        return value


class ComponentStatus(ForgeModel):
    """Safe cached status from one application-scoped provider."""

    id: Identifier = Field(description="Unique stable component/provider identifier.")
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=128)] = Field(
        description="Human-readable component name."
    )
    state: ComponentState = Field(description="Normalized component state.")
    capabilities: tuple[Identifier, ...] = Field(
        default=(), max_length=MAX_CAPABILITIES, description="Stable safe capabilities."
    )
    summary: BoundedText = Field(description="Safe bounded summary without raw producer data.")
    facts: tuple[StatusFact, ...] = Field(
        default=(), max_length=MAX_FACTS, description="Bounded scalar metadata only."
    )
    warnings: tuple[WarningCode, ...] = Field(
        default=(), max_length=MAX_WARNINGS, description="Safe categorized warnings."
    )
    stale: bool = Field(default=False, description="Whether cached observations may be stale.")
    observed_at: datetime = Field(description="UTC time at which this component cache was observed.")

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_utc(cls, value: datetime) -> datetime:
        return normalize_utc(value)

    @field_validator("capabilities")
    @classmethod
    def capabilities_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Component capabilities must be unique.")
        return value

    @field_validator("facts")
    @classmethod
    def facts_are_unique(cls, value: tuple[StatusFact, ...]) -> tuple[StatusFact, ...]:
        if len({fact.name for fact in value}) != len(value):
            raise ValueError("Component fact names must be unique.")
        return value


class ProjectStatus(ForgeModel):
    """Bounded, partial, non-transactional application workspace snapshot."""

    generated_at: datetime = Field(description="UTC aggregation completion time.")
    workspace_root: Annotated[str, StringConstraints(min_length=1, max_length=32)] = Field(
        description="Fixed configured marker; host workspace paths are never disclosed."
    )
    health: ProjectHealth = Field(description="Deterministic service-health classification.")
    activity: ProjectActivity = Field(description="Activity independent from service health.")
    components: tuple[ComponentStatus, ...] = Field(
        max_length=MAX_COMPONENTS, description="Successful provider snapshots in stable identifier order."
    )
    capabilities: tuple[Identifier, ...] = Field(
        max_length=MAX_CAPABILITIES, description="Sorted union of component capabilities."
    )
    warnings: tuple[WarningCode, ...] = Field(
        max_length=MAX_WARNINGS, description="Safe aggregate categories; no raw exceptions or output."
    )
    partial: bool = Field(
        description="Whether provider loss, a missing critical component, or deterministic truncation made the result partial."
    )
    failed_components: tuple[Identifier, ...] = Field(
        max_length=MAX_COMPONENTS,
        description="Stable identifiers whose provider failed validation or execution.",
    )
    timed_out_components: tuple[Identifier, ...] = Field(
        max_length=MAX_COMPONENTS, description="Stable identifiers whose provider deadline expired."
    )
    omitted_components: tuple[Identifier, ...] = Field(
        max_length=MAX_COMPONENTS,
        description="Stable identifiers deterministically omitted to enforce the response-size budget.",
    )
    response_truncated: bool = Field(
        description="Whether bounded aggregate fields or components were deterministically omitted."
    )

    @field_validator("generated_at")
    @classmethod
    def generated_at_is_utc(cls, value: datetime) -> datetime:
        return normalize_utc(value)


def utc_now() -> datetime:
    """Return an aware UTC timestamp for providers and aggregation."""

    return datetime.now(UTC)
