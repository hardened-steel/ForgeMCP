"""LSP byte framing stays inside the clangd tool specification."""

import json
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forgemcp.toolchain.errors import ToolParserError
from forgemcp.toolchain.tools.clangd import Connection, LspNotification, LspResponse


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_fragmented_utf8_frames_and_serialization_use_byte_lengths():
    values = [
        {"jsonrpc": "2.0", "id": 7, "result": "日本語 😀"},
        {"jsonrpc": "2.0", "method": "note", "params": {"text": "α"}},
    ]
    wire = b""
    for value in values:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        wire += f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body

    async def output() -> AsyncGenerator[SimpleNamespace]:
        yield SimpleNamespace(stream="stderr", text="ordinary log")
        for offset in range(0, len(wire), 3):
            yield SimpleNamespace(stream="stdout", text=wire[offset:offset + 3].decode("latin_1"))

    process = SimpleNamespace(
        process_id=7,
        output=output,
        wait=AsyncMock(return_value=0),
        write_stdin=AsyncMock(),
    )
    connection = Connection(process)
    messages = [message async for message in connection.messages()]
    assert messages == [LspResponse(7, "日本語 😀"), LspNotification("note", {"text": "α"})]
    await connection.send(messages[0])
    header, body = process.write_stdin.call_args.args[0].encode("latin_1").split(b"\r\n\r\n")
    assert header == f"Content-Length: {len(body)}".encode("ascii")
    assert json.loads(body) == values[0]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "wire",
    [
        b"Content-Length: wrong\r\n\r\n{}",
        b"Content-Length: 8\r\n\r\n{}",
    ],
)
async def test_invalid_or_incomplete_frames_raise_parser_error(wire):
    async def output() -> AsyncGenerator[SimpleNamespace]:
        yield SimpleNamespace(stream="stdout", text=wire.decode("latin_1"))

    process = SimpleNamespace(process_id=1, output=output, wait=AsyncMock(return_value=0))
    with pytest.raises(ToolParserError):
        _ = [message async for message in Connection(process).messages()]
