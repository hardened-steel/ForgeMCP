"""Strict internal records for incoming Debug Adapter Protocol messages."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from forgemcp.dap.errors import DapProtocolError


@dataclass(frozen=True, slots=True)
class DapResponse:
    """A validated adapter response retained only inside DAP/debugger layers."""

    request_seq: int
    command: str
    success: bool
    body: Mapping[str, object]
    message: str | None


@dataclass(frozen=True, slots=True)
class DapEvent:
    """A validated adapter event."""

    event: str
    body: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class DapReverseRequest:
    """A validated adapter-to-client request."""

    seq: int
    command: str
    arguments: Mapping[str, object]


def parse_message(message: Mapping[str, object]) -> DapResponse | DapEvent | DapReverseRequest:
    """Validate a DAP envelope without exposing raw unvalidated data."""
    message_type = message.get("type")
    _require_positive_integer(message.get("seq"), "sequence")
    if message_type == "response":
        request_seq = _require_positive_integer(message.get("request_seq"), "request sequence")
        command = _require_name(message.get("command"), "response command")
        success = message.get("success")
        if not isinstance(success, bool):
            raise DapProtocolError("The debug adapter sent a response without success status.")
        body = _mapping_or_empty(message.get("body"), "response body")
        raw_message = message.get("message")
        if raw_message is not None and not isinstance(raw_message, str):
            raise DapProtocolError("The debug adapter sent an invalid response message.")
        return DapResponse(request_seq, command, success, body, _bounded_message(raw_message))
    if message_type == "event":
        return DapEvent(_require_name(message.get("event"), "event name"), _mapping_or_empty(message.get("body"), "event body"))
    if message_type == "request":
        return DapReverseRequest(
            seq=_require_positive_integer(message.get("seq"), "request sequence"),
            command=_require_name(message.get("command"), "request command"),
            arguments=_mapping_or_empty(message.get("arguments"), "request arguments"),
        )
    raise DapProtocolError("The debug adapter sent an unknown DAP message type.")


def _require_positive_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DapProtocolError(f"The debug adapter sent an invalid {name}.")
    return value


def _require_name(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise DapProtocolError(f"The debug adapter sent an invalid {name}.")
    return value


def _mapping_or_empty(value: object, name: str) -> Mapping[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise DapProtocolError(f"The debug adapter sent a non-object {name}.")
    return dict(value)


def _bounded_message(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:512]
