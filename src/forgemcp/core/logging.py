"""Application-scoped structured logging with bounded sanitized fan-out."""

from __future__ import annotations

import inspect
import json
import re
import sys
import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol, TextIO


MAX_RECENT_LOG_EVENTS = 256
MAX_RECENT_LOG_BYTES = 512 * 1024
MAX_LOG_METADATA_FIELDS = 16
MAX_LOG_METADATA_STRING_CHARACTERS = 128

LOG_LEVELS = (
    "debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"
)
_LEVEL_PRIORITY = {name: index for index, name in enumerate(LOG_LEVELS)}
_STDERR_LEVELS = {
    "DEBUG": "debug",
    "INFO": "info",
    "WARNING": "warning",
    "ERROR": "error",
    "CRITICAL": "critical",
}

_SENSITIVE_KEY_PARTS = (
    "content", "password", "secret", "token", "authorization", "cookie",
    "path", "argv", "environment", "exception", "diagnostic", "command",
    "patch", "edit", "source", "pid", "handle",
)
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|(?:^|\s)/(?:[^\s/]+/)+)")
_SECRET_LIKE = re.compile(
    r"(?:password|passwd|secret|api[_-]?key|authorization|bearer|cookie|token)\s*[:=]",
    re.IGNORECASE,
)

# Categories are authored by ForgeMCP code. Unknown external/plugin categories
# collapse to one fixed value rather than becoming model-facing strings.
_ALLOWED_CATEGORIES = frozenset(
    {
        "application_started",
        "application_stopped",
        "plugin_failed_start_cleanup_failed",
        "plugin_stop_failed",
        "process_started",
        "process_finished",
        "workspace_patch_not_applied",
        "workspace_patch_applied",
        "workspace_text_edits_not_applied",
        "workspace_text_edits_applied",
        "workspace_temporary_cleanup_failed",
        "workspace_mutation_subscriber_degraded",
        "unclassified_event",
    }
)

_ALLOWED_METADATA_KEYS = frozenset(
    {
        "workspace_configured",
        "changed_files",
        "reason",
        "subscriber",
        "plugin_id",
        "failure_category",
        "exit_code",
        "timed_out",
        "stdout_characters",
        "stdout_truncated",
        "stderr_characters",
        "stderr_truncated",
        "ownership_required",
        "ownership_established",
    }
)


def _requires_redaction(value: Any) -> bool:
    """Keep exception text, secrets, and host paths out of operational records."""
    if isinstance(value, BaseException):
        return True
    return isinstance(value, str) and (
        _ABSOLUTE_PATH.search(value) is not None or _SECRET_LIKE.search(value) is not None
    )


def sanitize_log_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Legacy recursive sanitizer retained for callers and compatibility tests.

    ``StructuredLogger`` applies the stricter scalar allow-list below before an
    event reaches any sink. This helper remains useful for local unit-level
    normalization and never writes or sends data itself.
    """
    sanitized: dict[str, Any] = {}
    for key, value in context.items():
        if any(part in key.lower() for part in _SENSITIVE_KEY_PARTS) or _requires_redaction(value):
            sanitized[key] = "<redacted>"
        elif isinstance(value, Mapping):
            sanitized[key] = sanitize_log_context(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            sanitized[key] = value
        else:
            sanitized[key] = str(value)
    return sanitized


def _safe_metadata(context: Mapping[str, Any]) -> Mapping[str, str | int | float | bool | None]:
    sanitized: dict[str, str | int | float | bool | None] = {}
    for key in sorted(context):
        if len(sanitized) >= MAX_LOG_METADATA_FIELDS or key not in _ALLOWED_METADATA_KEYS:
            continue
        value = context[key]
        if isinstance(value, bool) or value is None:
            sanitized[key] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            sanitized[key] = max(-(2**63), min(2**63 - 1, value))
        elif isinstance(value, float) and value == value and abs(value) != float("inf"):
            sanitized[key] = value
        elif isinstance(value, str):
            if (
                len(value) > MAX_LOG_METADATA_STRING_CHARACTERS
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                or _requires_redaction(value)
            ):
                sanitized[key] = "<redacted>"
            else:
                sanitized[key] = value
    return MappingProxyType(sanitized)


@dataclass(frozen=True, slots=True)
class StructuredLogEvent:
    """One already-sanitized immutable event shared by every log sink."""

    sequence: int
    timestamp: str
    level: str
    logger: str
    category: str
    metadata: Mapping[str, str | int | float | bool | None]

    def as_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "level": self.level,
            "logger": self.logger,
            "category": self.category,
            "metadata": dict(self.metadata),
        }


class LogSink(Protocol):
    """Non-blocking transport-neutral destination for sanitized events."""

    def emit(self, event: StructuredLogEvent) -> None:
        """Accept one immutable sanitized event without reparsing stderr."""


class StderrLogSink:
    """Per-application compact JSON stderr sink with its own threshold."""

    def __init__(self, level: str, stream: TextIO | None = None) -> None:
        try:
            self._threshold = _LEVEL_PRIORITY[_STDERR_LEVELS[level.upper()]]
        except (AttributeError, KeyError) as error:
            raise ValueError("Stderr log level is invalid.") from error
        self._stream = sys.stderr if stream is None else stream
        self._lock = threading.Lock()

    def emit(self, event: StructuredLogEvent) -> None:
        if _LEVEL_PRIORITY[event.level] < self._threshold:
            return
        payload = event.as_dict()
        # ``event`` is the established stderr field; ``category`` is the
        # transport-neutral model-facing name retained in the shared event.
        payload["event"] = event.category
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self._stream.write(line + "\n")
            self._stream.flush()


class RecentLogRing:
    """Application-local 256-event/512-KiB sanitized retention ring."""

    def __init__(self) -> None:
        self._events: deque[tuple[StructuredLogEvent, int]] = deque()
        self._serialized_bytes = 0
        self._lock = threading.Lock()

    def emit(self, event: StructuredLogEvent) -> None:
        # Reserve two framing bytes per item. This conservatively includes the
        # separators/brackets needed to serialize the retained collection, so
        # the advertised 512-KiB bound is never exceeded by array framing.
        size = 2 + len(
            json.dumps(
                event.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )
        with self._lock:
            self._events.append((event, size))
            self._serialized_bytes += size
            while (
                len(self._events) > MAX_RECENT_LOG_EVENTS
                or self._serialized_bytes > MAX_RECENT_LOG_BYTES
            ):
                _, removed = self._events.popleft()
                self._serialized_bytes -= removed

    def snapshot(
        self, *, minimum_level: str = "debug", limit: int = 50
    ) -> tuple[StructuredLogEvent, ...]:
        """Return an immutable oldest-to-newest snapshot of the latest matches."""
        if minimum_level not in _LEVEL_PRIORITY:
            raise ValueError("Recent log level is invalid.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 256:
            raise ValueError("Recent log limit must be from 1 through 256.")
        threshold = _LEVEL_PRIORITY[minimum_level]
        with self._lock:
            matches = tuple(
                event for event, _ in self._events if _LEVEL_PRIORITY[event.level] >= threshold
            )
        return matches[-limit:]

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return self._serialized_bytes

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._serialized_bytes = 0


class StructuredLogger:
    """Application-owned fan-out; every sink receives the same sanitized value."""

    def __init__(self, stderr: StderrLogSink, recent: RecentLogRing) -> None:
        self._stderr = stderr
        self._recent = recent
        self._sinks: list[LogSink] = [stderr, recent]
        self._sequence = 0
        self._lock = threading.Lock()
        self._closed = False

    @property
    def recent(self) -> RecentLogRing:
        return self._recent

    def add_sink(self, sink: LogSink) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("Structured logger is closed.")
            if all(existing is not sink for existing in self._sinks):
                self._sinks.append(sink)

    def remove_sink(self, sink: LogSink) -> None:
        with self._lock:
            self._sinks = [existing for existing in self._sinks if existing is not sink]

    def debug(self, event: str, **context: Any) -> None:
        self._emit("debug", event, context)

    def info(self, event: str, **context: Any) -> None:
        self._emit("info", event, context)

    def notice(self, event: str, **context: Any) -> None:
        self._emit("notice", event, context)

    def warning(self, event: str, **context: Any) -> None:
        self._emit("warning", event, context)

    def error(self, event: str, **context: Any) -> None:
        self._emit("error", event, context)

    def critical(self, event: str, **context: Any) -> None:
        self._emit("critical", event, context)

    def alert(self, event: str, **context: Any) -> None:
        self._emit("alert", event, context)

    def emergency(self, event: str, **context: Any) -> None:
        self._emit("emergency", event, context)

    def _emit(self, level: str, category: str, context: Mapping[str, Any]) -> None:
        with self._lock:
            if self._closed:
                return
            self._sequence += 1
            event = StructuredLogEvent(
                sequence=self._sequence,
                timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                level=level,
                logger="forgemcp",
                category=category if category in _ALLOWED_CATEGORIES else "unclassified_event",
                metadata=_safe_metadata(context),
            )
            sinks = tuple(self._sinks)
        for sink in sinks:
            try:
                sink.emit(event)
            except Exception:
                # Logging is observational and cannot fail an operation or
                # recursively log its own transport failure.
                continue

    async def aclose(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            optional_sinks = tuple(self._sinks[2:])
            self._sinks = [self._stderr, self._recent]
        for sink in optional_sinks:
            close = getattr(sink, "aclose", None)
            if close is None:
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                continue
        self._recent.clear()


def create_logger(level: str, *, stream: TextIO | None = None) -> StructuredLogger:
    """Create an isolated stderr/ring fan-out without a global logging registry."""
    recent = RecentLogRing()
    return StructuredLogger(StderrLogSink(level, stream), recent)
