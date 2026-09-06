"""Process state shared by service consumers and MCP inspection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

ProcessStream = Literal["stdin", "stdout", "stderr"]
ProcessTimeoutMode = Literal["total", "idle"]
ProcessReadMode = Literal["combined", "protocol"]


class ProcessStatus(StrEnum):
    """Lifecycle state retained for a process."""

    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    TERMINATED = "terminated"
    TIMED_OUT = "timed_out"
    FAILED = "failed"


@dataclass(frozen=True)
class ProcessOutput:
    """One decoded chunk from a combined stdout/stderr stream."""

    stream: Literal["stdout", "stderr"]
    text: str


@dataclass(frozen=True)
class ProcessResult:
    """Terminal process state returned to a service consumer."""

    process_id: str
    status: ProcessStatus
    return_code: int | None
    duration: float
    error: str | None = None


@dataclass(frozen=True)
class ProcessLogEntry:
    """One text entry retained in the in-memory process transcript."""

    sequence: int
    timestamp: datetime
    stream: ProcessStream
    text: str


class ProcessSummary(BaseModel):
    """Model-visible summary of one tracked process."""

    process_id: str = Field(description="Stable identifier assigned by ForgeMCP.")
    pid: int | None = Field(
        description="Operating-system process identifier, when started."
    )
    executable: str = Field(description="Executable requested by the owning service.")
    arguments: list[str] = Field(description="Arguments passed to the executable.")
    shell: bool = Field(description="Whether the command runs through the native shell.")
    cwd: str = Field(description="Process working directory.")
    status: ProcessStatus = Field(description="Current process lifecycle state.")
    started_at: datetime = Field(description="UTC start timestamp.")
    finished_at: datetime | None = Field(description="UTC completion timestamp.")
    return_code: int | None = Field(description="Exit status when available.")
    timeout: float | None = Field(description="Configured timeout in seconds.")
    timeout_mode: ProcessTimeoutMode | None = Field(
        description="Total or output-idle timeout mode, or null when disabled."
    )
    encoding: str = Field(description="Encoding used for the retained text transcript.")
    had_decoding_errors: bool = Field(description="Whether replacement decoding occurred.")


class ProcessOverview(BaseModel):
    """Model-visible snapshot of tracked processes."""

    running: int = Field(description="Number of processes currently running or starting.")
    completed: int = Field(description="Number of processes in a terminal state.")
    processes: list[ProcessSummary] = Field(
        description="Processes matching the requested filter."
    )


class ProcessTranscriptItem(BaseModel):
    """One model-visible process transcript entry."""

    sequence: int
    timestamp: datetime
    stream: ProcessStream
    text: str


class ProcessDetails(ProcessSummary):
    """Complete retained state and transcript for one process."""

    error: str | None = Field(description="Sanitized lifecycle failure, when present.")
    transcript: list[ProcessTranscriptItem]
