"""Process state shared by service consumers and MCP inspection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.dataclasses import dataclass

ProcessStream = Literal["stdin", "stdout", "stderr"]


@dataclass(frozen=True)
class ProcessTimeout:
    """Optional total and idle limits in seconds for one process."""

    total: float | None = Field(
        description="All allowed time (configured in seconds) for process.",
        default=None,
        ge=0,
    )
    idle: float | None = Field(
        description="Max time (configured in seconds) without any chunks in stdout/stdin queues.",
        default=None,
        ge=0,
    )


@dataclass(frozen=True)
class ProcessEncoding:
    """Encoding options of process"""

    driver: str | Literal["default"]
    errors: Literal["strict", "replace"] = "replace"


@dataclass(frozen=True)
class ProcessOutput:
    """One decoded chunk from a combined stdout/stderr stream."""

    stream: Literal["stdout", "stderr"]
    text: str


@dataclass(frozen=True, kw_only=True)
class ProcessLogEntry:
    """One text entry retained in the in-memory process transcript."""

    timestamp: datetime = Field(
        description="When this log entry appeared.",
        default_factory=lambda: datetime.now(timezone.utc),
    )
    time: float = Field(repr=False)
    stream: Literal["stdout", "stderr", "stdin"]
    text: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class ProcessSummary:
    """Model-visible summary of one tracked process."""

    process_id: int = Field(description="Stable identifier assigned by ForgeMCP.")
    executable: str = Field(description="Executable requested by the owning service.")
    arguments: list[str] = Field(description="Arguments passed to the executable.")
    cwd: str = Field(description="Process working directory.")
    encoding: str = Field(description="Encoding used for the retained text transcript.")
    timeout: ProcessTimeout = Field(description="Configured timeout mode and duration.")


class ProcessStatus(BaseModel):
    """Current state of the process."""

    pid: int = Field(description="Operating-system process identifier.")
    started: datetime = Field(
        description="UTC start timestamp.",
        default_factory=lambda: datetime.now(timezone.utc),
    )
    work_time: float = Field(
        description="Process work time in seconds.",
        ge=0,
        default=0,
    )
    current_status: Literal[
        "interrupted", "running", "stopped", "stream_failure"
    ] | int = Field(
        description="Exit status or process status."
    )


class ProcessInfo(BaseModel):
    """Command and lifecycle snapshot without the potentially large transcript."""

    summary: ProcessSummary
    status: ProcessStatus


class ProcessOverview(BaseModel):
    """Model-visible snapshot of tracked processes."""

    running: int = Field(
        description="Number of processes currently running or starting."
    )
    completed: int = Field(description="Number of processes in a terminal state.")
    processes: list[ProcessInfo] = Field(
        description="Processes matching the requested filter."
    )


class ProcessDetails(BaseModel):
    """One immutable point-in-time view of a process and its ordered transcript."""

    process: ProcessInfo
    transcript: list[ProcessLogEntry]
    lines: tuple[int, int] | None = None


class FirstLines(BaseModel):
    """Select the first N lines."""

    model_config = ConfigDict(extra="forbid")
    first: int = Field(ge=1)


class LastLines(BaseModel):
    """Select the last N lines."""

    model_config = ConfigDict(extra="forbid")
    last: int = Field(ge=1)


class LineRange(BaseModel):
    """Select an inclusive absolute line range."""

    model_config = ConfigDict(extra="forbid")
    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_range(self) -> LineRange:
        """Reject inclusive line ranges whose end precedes the start."""
        if self.end < self.start:
            raise ValueError("Line range requires start <= end.")
        return self


class FirstSeconds(BaseModel):
    """Select the first N seconds from process start."""

    model_config = ConfigDict(extra="forbid")
    first: float = Field(gt=0, allow_inf_nan=False)


class LastSeconds(BaseModel):
    """Select the last N seconds of the process snapshot."""

    model_config = ConfigDict(extra="forbid")
    last: float = Field(gt=0, allow_inf_nan=False)


class TimeRange(BaseModel):
    """Seconds from process start; include start and exclude end."""

    model_config = ConfigDict(extra="forbid")
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_range(self) -> TimeRange:
        """Require a nonempty half-open interval in elapsed process seconds."""
        if self.end <= self.start:
            raise ValueError("Time range requires start < end.")
        return self
