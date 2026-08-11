"""Stable, transport-neutral data contracts for ForgeMCP domain modules."""

from forgemcp.models.diagnostics import Diagnostic, Severity
from forgemcp.models.files import FileChange, FileChangeKind, FileSnapshot, PatchResult
from forgemcp.models.locations import Location, Position, Range
from forgemcp.models.processes import MAX_PROCESS_OUTPUT_CHARACTERS, ProcessOutput, ProcessResult
from forgemcp.models.tasks import TaskResult, TaskState

__all__ = [
    "Diagnostic",
    "FileChange",
    "FileChangeKind",
    "FileSnapshot",
    "Location",
    "MAX_PROCESS_OUTPUT_CHARACTERS",
    "PatchResult",
    "Position",
    "ProcessOutput",
    "ProcessResult",
    "Range",
    "Severity",
    "TaskResult",
    "TaskState",
]
