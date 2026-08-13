"""Bounded DAP Content-Length framing, independent of application services."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

from forgemcp.dap.errors import DapProtocolError

MAX_DAP_HEADER_BYTES = 8 * 1024
MAX_DAP_MESSAGE_BYTES = 1024 * 1024


async def read_message(
    reader: asyncio.StreamReader,
    *,
    max_message_bytes: int = MAX_DAP_MESSAGE_BYTES,
    max_header_bytes: int = MAX_DAP_HEADER_BYTES,
) -> Mapping[str, object]:
    """Read exactly one strictly framed DAP JSON object.

    ``StreamReader`` retains excess bytes, so fragmented frames and several
    messages supplied in one read are handled without a private byte buffer.
    """
    try:
        headers = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError as error:
        raise DapProtocolError("The debug adapter sent oversized message headers.") from error
    if len(headers) > max_header_bytes:
        raise DapProtocolError("The debug adapter sent oversized message headers.")
    content_length: int | None = None
    for line in headers[:-4].split(b"\r\n"):
        if not line or b"\x00" in line:
            raise DapProtocolError("The debug adapter sent malformed message headers.")
        name, delimiter, raw_value = line.partition(b":")
        if not delimiter or not name or not raw_value:
            raise DapProtocolError("The debug adapter sent malformed message headers.")
        try:
            header_name = name.decode("ascii").strip().casefold()
            value = raw_value.decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise DapProtocolError("The debug adapter sent non-ASCII message headers.") from error
        if header_name == "content-length":
            if content_length is not None or not value.isdecimal():
                raise DapProtocolError("The debug adapter sent an invalid Content-Length header.")
            content_length = int(value)
        elif header_name != "content-type":
            raise DapProtocolError("The debug adapter sent an unsupported message header.")
    if content_length is None or content_length > max_message_bytes:
        raise DapProtocolError("The debug adapter sent a message outside the configured size limit.")
    try:
        payload = await reader.readexactly(content_length)
        parsed = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise DapProtocolError("The debug adapter sent non-UTF-8 JSON data.") from error
    except json.JSONDecodeError as error:
        raise DapProtocolError("The debug adapter sent malformed JSON data.") from error
    if not isinstance(parsed, dict):
        raise DapProtocolError("The debug adapter sent a non-object DAP message.")
    return parsed


def frame_message(message: Mapping[str, object], *, max_message_bytes: int = MAX_DAP_MESSAGE_BYTES) -> bytes:
    """Encode one mapping as a bounded Content-Length DAP message."""
    try:
        payload = json.dumps(dict(message), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise DapProtocolError("ForgeMCP could not encode a DAP message.") from error
    if len(payload) > max_message_bytes:
        raise DapProtocolError("The outbound DAP message exceeds the configured size limit.")
    return b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload
