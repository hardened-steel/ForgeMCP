"""Structured logging that protects file contents and common secrets."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from typing import Any

_SENSITIVE_KEY_PARTS = (
    "content", "password", "secret", "token", "authorization", "cookie",
    "path", "argv", "environment", "exception", "error",
)
_ABSOLUTE_PATH = re.compile(r"(?:^[A-Za-z]:[\\/]|^\\\\|^/)")


def _requires_redaction(value: Any) -> bool:
    """Keep exception text and host paths out of operational records."""
    if isinstance(value, BaseException):
        return True
    return isinstance(value, str) and _ABSOLUTE_PATH.search(value) is not None


def sanitize_log_context(context: Mapping[str, Any]) -> dict[str, Any]:
    """Redact fields that could carry source code, file contents, or credentials."""
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


class JsonFormatter(logging.Formatter):
    """Emit a compact JSON record suitable for stderr collection."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "event": record.getMessage(),
            **getattr(record, "forgemcp_context", {}),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)


class StructuredLogger:
    """Minimal event logger; use fields, never interpolated source text."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def info(self, event: str, **context: Any) -> None:
        self._logger.info(event, extra={"forgemcp_context": sanitize_log_context(context)})

    def warning(self, event: str, **context: Any) -> None:
        self._logger.warning(event, extra={"forgemcp_context": sanitize_log_context(context)})


def create_logger(level: str) -> StructuredLogger:
    """Create the named ForgeMCP stderr logger without changing root logging."""
    logger = logging.getLogger("forgemcp")
    logger.setLevel(level)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return StructuredLogger(logger)
