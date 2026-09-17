"""Process state shared by service consumers and MCP inspection."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field
from pydantic.dataclasses import dataclass


ProcessStream = Literal["stdin", "stdout", "stderr"]

@dataclass(frozen=True)
class ProcessTimeout:
    total: float | None = Field(
        description="All allowed time (configured in seconds) for process.",
        default=None,
        ge=0
    )
    idle: float | None = Field(
        description="Max time (configured in seconds) without any chunks in stdout/stdin queues.",
        default=None,
        ge=0
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

    timestamp: datetime = Field(description="When this log entry appeared.", default_factory=lambda: datetime.now(timezone.utc))
    time: float = Field(repr=False)
    stream: Literal["stdout", "stderr", "stdin"]
    text: str


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
    started: datetime = Field(description="UTC start timestamp.", default_factory=lambda: datetime.now(timezone.utc))
    work_time: float = Field(description="Process work time in seconds.", ge=0, default=0)
    current_status: Literal["interrupted", "running"] | int = Field(description="Exit status or process status.")
    transcript: list[ProcessLogEntry] = Field(description="Process log. All communications will be saved here.", default_factory=list)


class ProcessOverview(BaseModel):
    """Model-visible snapshot of tracked processes."""

    running: int = Field(description="Number of processes currently running or starting.")
    completed: int = Field(description="Number of processes in a terminal state.")
    processes: list[ProcessSummary] = Field(
        description="Processes matching the requested filter."
    )
