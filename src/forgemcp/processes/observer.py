"""Bounded observation contracts for short ProcessRuntime commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


MAX_PROCESS_OBSERVER_CHUNK_CHARACTERS = 4_096
"""Maximum decoded text supplied to an observer for one pipe read."""

ProcessStream = Literal["stdout", "stderr"]


@dataclass(frozen=True, slots=True)
class ProcessOutputEvent:
    """One bounded raw observation event, never a public MCP result model.

    No cross-stream ordering is implied.  Observers are intended for local,
    trusted parsers that derive fixed safe progress phases; they must not pass
    this text through to an MCP client or persistent logs.
    """

    stream: ProcessStream
    text: str
    observed_at: datetime
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.stream not in {"stdout", "stderr"}:
            raise ValueError("Process observation streams are stdout or stderr.")
        if not isinstance(self.text, str) or len(self.text) > MAX_PROCESS_OBSERVER_CHUNK_CHARACTERS:
            raise ValueError("Process observation chunks must be bounded text.")
        if not isinstance(self.truncated, bool):
            raise TypeError("Process observation truncation flags must be boolean.")


class ProcessOutputObserver(Protocol):
    """Trusted, local callback for a bounded short-command output event."""

    def __call__(self, event: ProcessOutputEvent) -> object | Awaitable[object]:
        """Observe output without blocking a pipe reader or exposing it externally."""

