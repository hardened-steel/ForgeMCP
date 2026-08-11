"""Bounded process-capture models independent of a process runtime."""

from __future__ import annotations

from datetime import datetime

from pydantic import ConfigDict, Field, field_validator, model_validator

from forgemcp.models._base import ForgeModel, normalize_utc

MAX_PROCESS_OUTPUT_CHARACTERS = 65_536
"""Largest stdout or stderr text payload accepted by ``ProcessOutput``."""


class ProcessOutput(ForgeModel):
    """One captured process stream, capped before it reaches the domain boundary."""

    # Process output is opaque text, not an identifier or a user-entered label:
    # preserving leading/trailing whitespace (including diagnostics' newlines)
    # is part of its contract.
    model_config = ConfigDict(str_strip_whitespace=False)

    text: str = Field(
        max_length=MAX_PROCESS_OUTPUT_CHARACTERS,
        description="Captured text, limited to 65,536 Unicode code points per stream.",
    )
    truncated: bool = Field(
        default=False,
        description="Whether capture discarded additional output after the size limit.",
    )

    def log_summary(self) -> dict[str, bool | int]:
        """Return metadata safe for structured logs without exposing captured text."""
        return {"characters": len(self.text), "truncated": self.truncated}


class ProcessResult(ForgeModel):
    """Immutable process outcome with separate, bounded stdout and stderr captures."""

    exit_code: int | None = Field(
        default=None,
        description="Process exit code, or null when a timeout ended execution.",
    )
    timed_out: bool = Field(
        default=False,
        description="Whether execution was ended because its configured timeout elapsed.",
    )
    started_at: datetime = Field(description="UTC instant at which process execution began.")
    finished_at: datetime = Field(description="UTC instant at which process execution ended.")
    stdout: ProcessOutput = Field(description="Captured standard-output stream.")
    stderr: ProcessOutput = Field(description="Captured standard-error stream.")

    @field_validator("started_at", "finished_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: datetime) -> datetime:
        """Store all process timestamps as UTC-aware datetimes."""
        return normalize_utc(value)

    @model_validator(mode="after")
    def must_have_consistent_completion_state(self) -> "ProcessResult":
        """Keep process completion time and exit-code semantics unambiguous."""
        if self.finished_at < self.started_at:
            raise ValueError("Process finished_at must not precede started_at.")
        if self.timed_out and self.exit_code is not None:
            raise ValueError("Timed-out processes must not expose an exit code.")
        if not self.timed_out and self.exit_code is None:
            raise ValueError("Completed processes must expose an exit code.")
        return self
