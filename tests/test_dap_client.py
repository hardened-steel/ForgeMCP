"""Deterministic gate tests for the production DAP stream transport."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from forgemcp.dap import (
    DapClient,
    DapClientState,
    DapConnectionClosedError,
    DapProtocolError,
    DapRequestError,
    DapRequestTimeoutError,
)
from forgemcp.dap.errors import DapRequestCompatibility
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.processes import ProcessPolicy, ProcessRuntime


def _frame(value: object) -> bytes:
    payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload


def _messages(data: bytes) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    remaining = data
    while remaining:
        headers, rest = remaining.split(b"\r\n\r\n", 1)
        length = int(headers.split(b":", 1)[1])
        payload, remaining = rest[:length], rest[length:]
        values.append(json.loads(payload.decode("utf-8")))
    return values


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def test_client_handles_partial_frames_out_of_order_responses_and_interleaved_events():
    async def exercise() -> None:
        received: list[tuple[str, dict[str, object]]] = []
        reader = asyncio.StreamReader()
        writer = _Writer()

        async def event(name: str, body: dict[str, object]) -> None:
            received.append((name, body))

        client = DapClient(reader, writer, event_handler=event)  # type: ignore[arg-type]
        await client.start()
        first = asyncio.create_task(client.request("first"))
        second = asyncio.create_task(client.request("second"))
        await asyncio.sleep(0.01)
        assert [message["seq"] for message in _messages(bytes(writer.data))] == [1, 2]
        inbound = (
            _frame({"seq": 4, "type": "response", "request_seq": 2, "command": "second", "success": True, "body": {"value": "two"}})
            + _frame({"seq": 5, "type": "event", "event": "output", "body": {"output": "not logged"}})
            + _frame({"seq": 6, "type": "response", "request_seq": 1, "command": "first", "success": True, "body": {"value": "one"}})
        )
        reader.feed_data(inbound[:17])
        reader.feed_data(inbound[17:83])
        reader.feed_data(inbound[83:])
        assert await first == {"value": "one"}
        assert await second == {"value": "two"}
        await asyncio.sleep(0)
        assert received == [("output", {"output": "not logged"})]
        await client.aclose()

    asyncio.run(exercise())


def test_client_classifies_only_exact_bounded_pause_thread_compatibility_response():
    async def rejected(body: object) -> DapRequestError:
        reader = asyncio.StreamReader()
        writer = _Writer()
        client = DapClient(reader, writer)  # type: ignore[arg-type]
        await client.start()
        request = asyncio.create_task(client.request("pause"))
        await asyncio.sleep(0)
        reader.feed_data(
            _frame({"seq": 2, "type": "response", "request_seq": 1, "command": "pause", "success": False, "body": body})
        )
        with pytest.raises(DapRequestError) as caught:
            await request
        await client.aclose()
        return caught.value

    exact = asyncio.run(rejected({"error": {"format": "Request 'pause': missing value at arguments.threadId", "id": 3, "showUser": True}}))
    assert exact.compatibility is DapRequestCompatibility.PAUSE_THREAD_ID_REQUIRED
    assert "missing value" not in str(exact)
    arbitrary = asyncio.run(rejected({"error": {"format": "threadId missing after adapter failure", "id": 3, "showUser": True}}))
    assert arbitrary.compatibility is None


def test_reverse_requests_are_denied_and_writes_remain_sequential():
    async def exercise() -> None:
        reader = asyncio.StreamReader()
        writer = _Writer()
        client = DapClient(reader, writer)  # type: ignore[arg-type]
        await client.start()
        reader.feed_data(
            _frame({"seq": 11, "type": "request", "command": "runInTerminal", "arguments": {"args": ["unsafe"]}})
            + _frame({"seq": 12, "type": "request", "command": "startDebugging", "arguments": {}})
            + _frame({"seq": 13, "type": "request", "command": "other", "arguments": {}})
        )
        await asyncio.sleep(0.05)
        replies = _messages(bytes(writer.data))
        assert [reply["request_seq"] for reply in replies] == [11, 12, 13]
        assert all(reply["success"] is False for reply in replies)
        assert "runInTerminal is disabled" in str(replies[0]["message"])
        assert "startDebugging is disabled" in str(replies[1]["message"])
        await client.aclose()

    asyncio.run(exercise())


def test_timeout_and_cancellation_send_dap_cancel_only_after_initialize_capability():
    async def exercise() -> None:
        reader = asyncio.StreamReader()
        writer = _Writer()
        client = DapClient(reader, writer, default_timeout_seconds=0.01)  # type: ignore[arg-type]
        await client.start()
        initializing = asyncio.create_task(client.request("initialize"))
        await asyncio.sleep(0)
        reader.feed_data(
            _frame({"seq": 1, "type": "response", "request_seq": 1, "command": "initialize", "success": True, "body": {"supportsCancelRequest": True}})
        )
        assert await initializing == {"supportsCancelRequest": True}
        with pytest.raises(DapRequestTimeoutError):
            await client.request("slow")
        assert any(message["command"] == "cancel" for message in _messages(bytes(writer.data)))
        pending = asyncio.create_task(client.request("cancelled", timeout_seconds=1.0))
        await asyncio.sleep(0.01)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.sleep(0.01)
        assert sum(message["command"] == "cancel" for message in _messages(bytes(writer.data))) == 2
        await client.aclose()

    asyncio.run(exercise())


def test_malformed_data_and_eof_fail_every_pending_future_without_payload_leakage():
    async def exercise() -> None:
        reader = asyncio.StreamReader()
        writer = _Writer()
        client = DapClient(reader, writer)  # type: ignore[arg-type]
        await client.start()
        pending = asyncio.create_task(client.request("waiting"))
        await asyncio.sleep(0)
        reader.feed_data(b"Content-Length: 1\r\n\r\n{")
        with pytest.raises(DapProtocolError):
            await pending
        assert client.state is DapClientState.FAILED
        await client.aclose()

        eof_reader = asyncio.StreamReader()
        eof_writer = _Writer()
        eof_client = DapClient(eof_reader, eof_writer)  # type: ignore[arg-type]
        await eof_client.start()
        eof_reader.feed_eof()
        await asyncio.sleep(0)
        with pytest.raises(DapConnectionClosedError):
            await eof_client.request("after_eof")
        await eof_client.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "frame",
    (
        b"Content-Length: 1\r\nContent-Length: 1\r\n\r\n{}",
        b"Content-Length: -1\r\n\r\n{}",
        b"Content-Length: 0\r\n\r\n",
        b"Content-Length: 1048577\r\n\r\n{}",
        b"X-Unknown: value\r\nContent-Length: 2\r\n\r\n{}",
        b"Content-Length: 2\r\n\r\n\xff\xff",
        b"Content-Length: 2\r\n\r\n[]",
    ),
)
def test_invalid_framing_is_a_bounded_client_failure(frame: bytes):
    async def exercise() -> None:
        reader = asyncio.StreamReader()
        writer = _Writer()
        client = DapClient(reader, writer)  # type: ignore[arg-type]
        await client.start()
        reader.feed_data(frame)
        await asyncio.sleep(0.01)
        assert client.state is DapClientState.FAILED
        assert isinstance(client.failure, DapProtocolError)
        assert writer.closed is True
        assert client._event_task is None
        assert client._reverse_tasks == set()

    asyncio.run(exercise())


def test_oversized_partial_header_and_response_command_mismatch_close_safely():
    async def exercise() -> None:
        reader = asyncio.StreamReader()
        writer = _Writer()
        client = DapClient(reader, writer)  # type: ignore[arg-type]
        await client.start()
        reader.feed_data(b"A" * 8193)
        await asyncio.sleep(0.01)
        assert client.state is DapClientState.FAILED
        assert isinstance(client.failure, DapProtocolError)

        reader = asyncio.StreamReader()
        writer = _Writer()
        client = DapClient(reader, writer)  # type: ignore[arg-type]
        await client.start()
        pending = asyncio.create_task(client.request("threads"))
        await asyncio.sleep(0)
        reader.feed_data(_frame({"seq": 1, "type": "response", "request_seq": 1, "command": "stackTrace", "success": True, "body": {}}))
        with pytest.raises(DapProtocolError):
            await pending
        await asyncio.sleep(0.01)
        assert client.state is DapClientState.FAILED
        assert writer.closed is True

    asyncio.run(exercise())


def test_bounded_event_and_reverse_request_workers_reject_floods_without_task_growth():
    async def exercise() -> None:
        reader = asyncio.StreamReader()
        writer = _Writer()

        async def slow_event(_: str, __: dict[str, object]) -> None:
            await asyncio.Event().wait()

        client = DapClient(reader, writer, event_handler=slow_event)  # type: ignore[arg-type]
        await client.start()
        reader.feed_data(b"".join(
            _frame({"seq": index + 1, "type": "event", "event": "output", "body": {}})
            for index in range(17)
        ))
        await asyncio.sleep(0.02)
        assert client.state is DapClientState.FAILED
        assert isinstance(client.failure, DapProtocolError)
        assert client._event_task is None
        assert client._reverse_tasks == set()

        reader = asyncio.StreamReader()
        writer = _Writer()
        client = DapClient(reader, writer)  # type: ignore[arg-type]
        await client.start()
        reader.feed_data(b"".join(
            _frame({"seq": index + 1, "type": "request", "command": "unknown", "arguments": {}})
            for index in range(9)
        ))
        await asyncio.sleep(0.02)
        assert client.state is DapClientState.FAILED
        assert isinstance(client.failure, DapProtocolError)
        assert client._reverse_tasks == set()

    asyncio.run(exercise())


def test_transport_gate_uses_a_fake_dap_subprocess_only_through_process_runtime(tmp_path: Path):
    """Exercise real pipes while keeping the production client process-neutral."""
    fake_adapter = r'''
import json
import sys

def receive():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.casefold()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])).decode("utf-8"))

def send(value):
    body = json.dumps(value, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body)
    sys.stdout.buffer.flush()

sequence = 10
while True:
    request = receive()
    if request is None:
        break
    command = request.get("command")
    if command == "initialize":
        send({"seq": sequence, "type": "response", "request_seq": request["seq"], "command": command, "success": True, "body": {"supportsCancelRequest": True}})
        sequence += 1
    elif command == "disconnect":
        send({"seq": sequence, "type": "response", "request_seq": request["seq"], "command": command, "success": True})
        break
    else:
        send({"seq": sequence, "type": "event", "event": "output", "body": {"category": "console", "output": "fake"}})
        sequence += 1
        send({"seq": sequence, "type": "response", "request_seq": request["seq"], "command": command, "success": True, "body": {"command": command}})
        sequence += 1
'''

    async def exercise() -> None:
        runtime = ProcessRuntime(
            ForgeConfig(workspace_root=tmp_path),
            create_logger("CRITICAL"),
            policy=ProcessPolicy(
                allowed_executables=frozenset(),
                allowed_executable_paths=frozenset({Path(sys.executable).resolve()}),
                default_timeout_seconds=2.0,
                maximum_timeout_seconds=5.0,
            ),
        )
        handle = await runtime.start([sys.executable, "-u", "-c", fake_adapter])
        events: list[str] = []

        async def event(name: str, _: dict[str, object]) -> None:
            events.append(name)

        client = DapClient(handle.stdout, handle.stdin, event_handler=event)
        await client.start()
        assert (await client.request("initialize"))["supportsCancelRequest"] is True
        assert await client.request("threads") == {"command": "threads"}
        assert await client.request("disconnect") == {}
        await asyncio.sleep(0)
        assert events == ["output"]
        await client.aclose(expected_eof=True)
        await handle.aclose()
        await runtime.aclose()

    asyncio.run(exercise())
