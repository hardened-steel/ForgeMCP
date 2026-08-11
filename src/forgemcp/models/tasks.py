"""Models for the observable outcome of long-running ForgeMCP work."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from forgemcp.models._base import ForgeModel, normalize_utc


class TaskState(StrEnum):
    """Lifecycle states shared by background operations."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskResult(ForgeModel):
    """The final, immutable result of one background task."""

    task_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
        description="Stable, opaque identifier assigned to the task.",
    )
    state: TaskState = Field(description="Terminal state reached by the task.")
    started_at: datetime = Field(description="UTC instant at which task execution began.")
    finished_at: datetime = Field(description="UTC instant at which the task reached its terminal state.")
    summary: str | None = Field(
        default=None,
        max_length=4096,
        description="Optional bounded human-readable outcome summary.",
    )

    @field_validator("started_at", "finished_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: datetime) -> datetime:
        """Store all task timestamps as UTC-aware datetimes."""
        return normalize_utc(value)

    @model_validator(mode="after")
    def must_describe_a_finished_task(self) -> "TaskResult":
        """Reject non-terminal task states and reversed execution intervals."""
        if self.state in {TaskState.PENDING, TaskState.RUNNING}:
            raise ValueError("TaskResult requires a terminal task state.")
        if self.finished_at < self.started_at:
            raise ValueError("Task finished_at must not precede started_at.")
        return self
