"""Deterministic framing and JSON-RPC tests for the shared LSP transport."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from forgemcp.lsp import LspClient, LspClientState, LspConnectionClosedError, LspProtocolError, LspRequestTimeoutError
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.processes import ProcessPolicy, ProcessRuntime


def _frame(value: object) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _request_ids(data: bytes) -> list[int]:
    ids: list[int] = []
    remaining = data
    while remaining:
        header, body = remaining.split(b"\r\n\r\n", 1)
        length = int(header.split(b":", 1)[1])
        payload, remaining = body[:length], body[length:]
        value = json.loads(payload)
        if "id" in value and "method" in value:
            ids.append(value["id"])
    return ids


def test_lsp_client_frames_partial_reads_and_matches_out_of_order_responses():
    async def exercise() -> None:
        reader = asyncio.StreamReader()
        writer = _Writer()
        client = LspClient(reader, writer)  # type: ignore[arg-type]
        await client.start()
        first = asyncio.create_task(client.request("first"))
        second = asyncio.create_task(client.request("second"))
        await asyncio.sleep(0)
        ids = _request_ids(bytes(writer.data))
        assert ids == [1, 2]
        response = _frame({"jsonrpc": "2.0", "id": 2, "result": "two"}) + _frame(
            {"jsonrpc": "2.0", "id": 1, "result": "one"}
        )
        reader.feed_data(response[:11])
        reader.feed_data(response[11:])
        assert await first == "one"
        assert await second == "two"
        await client.aclose()

    asyncio.run(exercise())


def test_lsp_client_timeout_sends_cancel_and_handles_malformed_or_eof_streams():
    async def exercise() -> None:
        reader = asyncio.StreamReader()
        writer = _Writer()
        client = LspClient(reader, writer, default_timeout_seconds=0.01)  # type: ignore[arg-type]
        await client.start()
        with pytest.raises(LspRequestTimeoutError):
            await client.request("slow")
        assert b"$/cancelRequest" in writer.data
        reader.feed_data(b"Content-Length: 1\r\n\r\n{")
        await asyncio.sleep(0)
        assert client.state is LspClientState.FAILED
        with pytest.raises(LspProtocolError):
            await client.request("after_failure")
        await client.aclose()

        eof_reader = asyncio.StreamReader()
        eof_writer = _Writer()
        eof_client = LspClient(eof_reader, eof_writer)  # type: ignore[arg-type]
        await eof_client.start()
        eof_reader.feed_eof()
        await asyncio.sleep(0)
        with pytest.raises(LspConnectionClosedError):
            await eof_client.request("after_eof")
        await eof_client.aclose()

    asyncio.run(exercise())


def test_lsp_client_protocol_test_uses_a_fake_clangd_subprocess_via_process_runtime(tmp_path: Path):
    """Exercise a real child pipe without letting tests bypass ProcessRuntime."""
    fake_clangd = r'''
import json
import sys

def read_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        name, value = line.decode("ascii").split(":", 1)
        headers[name.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])).decode("utf-8"))

def send(value):
    data = json.dumps(value, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(data)).encode("ascii") + b"\r\n\r\n" + data)
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    if message is None:
        break
    if message.get("method") == "initialize":
        send({"jsonrpc":"2.0", "id":message["id"], "result":{"capabilities":{"positionEncoding":"utf-16"}}})
    elif message.get("method") == "shutdown":
        send({"jsonrpc":"2.0", "id":message["id"], "result":None})
    elif message.get("method") == "exit":
        break
'''

    async def exercise() -> None:
        config = ForgeConfig(workspace_root=tmp_path)
        runtime = ProcessRuntime(
            config,
            create_logger("CRITICAL"),
            policy=ProcessPolicy(
                allowed_executables=frozenset(),
                allowed_executable_paths=frozenset({Path(sys.executable).resolve()}),
                default_timeout_seconds=2.0,
                maximum_timeout_seconds=5.0,
            ),
        )
        handle = await runtime.start([sys.executable, "-u", "-c", fake_clangd])
        client = LspClient(handle.stdout, handle.stdin)
        await client.start()
        initialized = await client.request("initialize", {})
        assert initialized == {"capabilities": {"positionEncoding": "utf-16"}}
        assert await client.request("shutdown", {}) is None
        await client.notify("exit", {})
        await client.aclose()
        assert await handle.wait(timeout_seconds=2.0) == 0
        await runtime.aclose()

    asyncio.run(exercise())
