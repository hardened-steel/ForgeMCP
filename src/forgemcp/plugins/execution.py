"""Request-scoped, transport-neutral tool execution contracts.

This module intentionally has no MCP SDK dependency.  The server adapter owns
the conversion from an SDK request into one of these small values; feature
modules may receive it for the lifetime of a single handler invocation only.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ABSOLUTE_PATH = re.compile(r"(?:^[A-Za-z]:[\\/]|^[/\\]|^\\\\)")
_WHITESPACE = re.compile(r"\s+")
_SECRET_LABEL = re.compile(r"\b(?:api[ _-]?key|authorization|password|secret|token)\b", re.IGNORECASE)
MAX_PROGRESS_MESSAGE_CHARACTERS = 160
"""Largest normalized progress label accepted by the public contract."""


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """One bounded, client-safe request-progress notification.

    ``total`` is omitted for phase/activity notifications.  It is supplied
    only when the producer knows the value exactly (for example Ninja's
    ``[done/total]`` notation).  ``terminal`` is adapter metadata, not MCP
    payload data; it permits a final success/failure/cancellation state to
    bypass normal rate limiting without creating background notification
    tasks.
    """

    progress: float
    total: float | None = None
    message: str = "Working"
    terminal: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.progress, bool) or not isinstance(self.progress, (int, float)):
            raise TypeError("Progress values must be finite numbers.")
        if not math.isfinite(float(self.progress)) or float(self.progress) < 0:
            raise ValueError("Progress values must be finite and non-negative.")
        if self.total is not None:
            if isinstance(self.total, bool) or not isinstance(self.total, (int, float)):
                raise TypeError("Progress totals must be finite numbers when supplied.")
            if not math.isfinite(float(self.total)) or float(self.total) <= 0:
                raise ValueError("Progress totals must be finite and greater than zero.")
            if float(self.progress) > float(self.total):
                raise ValueError("Progress must not exceed its total.")
        if not isinstance(self.message, str):
            raise TypeError("Progress messages must be strings.")
        normalized = _WHITESPACE.sub(" ", self.message.strip())
        if (
            not normalized
            or len(normalized) > MAX_PROGRESS_MESSAGE_CHARACTERS
            or _CONTROL.search(normalized)
            or _ABSOLUTE_PATH.search(normalized)
            or _SECRET_LABEL.search(normalized)
            or "=" in normalized
            or "/" in normalized
            or "\\" in normalized
        ):
            raise ValueError("Progress messages must be short normalized labels without paths or control text.")
        if not isinstance(self.terminal, bool):
            raise TypeError("Progress terminal markers must be boolean.")
        object.__setattr__(self, "progress", float(self.progress))
        object.__setattr__(self, "total", None if self.total is None else float(self.total))
        object.__setattr__(self, "message", normalized)


class ProgressReporter(Protocol):
    """Transport-neutral sink for progress from one tool invocation."""

    @property
    def supports_progress(self) -> bool:
        """Whether the active client can receive progress notifications."""

    async def report(self, update: ProgressUpdate) -> None:
        """Best-effort delivery of one validated update; never alter the operation outcome."""


class NoOpProgressReporter:
    """Reporter used for in-process calls and MCP clients without a token."""

    __slots__ = ()

    @property
    def supports_progress(self) -> bool:
        return False

    async def report(self, update: ProgressUpdate) -> None:
        # Validate at the contract boundary even when the client does not
        # support progress, while intentionally doing no I/O or task creation.
        if not isinstance(update, ProgressUpdate):
            raise TypeError("Progress reporters accept ProgressUpdate values only.")


def _current_task_cancelled() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    """Ephemeral execution capabilities passed only to an opted-in handler.

    The context must not be stored in application services or plugin state.
    Cancellation is still delivered through normal ``asyncio.CancelledError``;
    ``is_cancelled`` and ``throw_if_cancelled`` allow a handler to check before
    beginning a long phase without importing a transport type.
    """

    progress_reporter: ProgressReporter = field(default_factory=NoOpProgressReporter)
    _is_cancelled: Callable[[], bool] = field(default=_current_task_cancelled, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not hasattr(self.progress_reporter, "report") or not hasattr(self.progress_reporter, "supports_progress"):
            raise TypeError("ToolExecutionContext requires a ProgressReporter.")
        if not callable(self._is_cancelled):
            raise TypeError("ToolExecutionContext cancellation callback must be callable.")

    @property
    def supports_progress(self) -> bool:
        """Return the active client's progress capability without exposing its transport."""
        return bool(self.progress_reporter.supports_progress)

    def is_cancelled(self) -> bool:
        """Return whether cancellation has already been requested for this call."""
        return bool(self._is_cancelled())

    def throw_if_cancelled(self) -> None:
        """Raise normal asyncio cancellation before beginning a new operation phase."""
        if self.is_cancelled():
            raise asyncio.CancelledError()

    async def report_progress(self, update: ProgressUpdate) -> None:
        """Report one safe update through the request-owned reporter."""
        if not isinstance(update, ProgressUpdate):
            raise TypeError("Tool execution contexts accept ProgressUpdate values only.")
        try:
            await self.progress_reporter.report(update)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A third-party/in-process reporter is observational only.  The
            # actual tool operation must retain its normal result/error path.
            return
