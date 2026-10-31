"""LLDB-DAP envelope validation, bounded framing, and process ownership."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from forgemcp.process.errors import ProcessStartError, ProcessStreamError
from forgemcp.process.models import ProcessEncoding, ProcessOutput, ProcessTimeout
from forgemcp.toolchain.errors import ToolCommandError, ToolParserError
from forgemcp.toolchain.tools.lldb_dap import (
    Connection,
    DapEvent,
    DapRequest,
    DapResponse,
    Methods,
    create_spec,
    parse_message,
)


@pytest.fixture
def anyio_backend():
    """Use the production asyncio process transport."""
    return "asyncio"


def frame(value: object) -> bytes:
    """Serialize a test envelope using the DAP UTF-8 byte-counted wire format."""
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def process_with_output(wire: bytes, *, code: int = 0) -> SimpleNamespace:
    """Expose fragmented protocol output and observable process operations."""
    async def output() -> AsyncGenerator[ProcessOutput]:
        """Interleave ordinary stderr with small losslessly decoded stdout chunks."""
        yield ProcessOutput("stderr", "adapter diagnostics")
        for offset in range(0, len(wire), 3):
            yield ProcessOutput("stdout", wire[offset:offset + 3].decode("latin_1"))

    return SimpleNamespace(
        process_id=17,
        output=output,
        wait=AsyncMock(return_value=code),
        close_stdin=AsyncMock(),
        write_stdin=AsyncMock(),
        close=AsyncMock(),
    )


@pytest.mark.anyio
async def test_fragmented_messages_and_unicode_preserve_all_envelope_types():
    """Decode multiple frames independently of chunk boundaries and unrelated stderr."""
    values = [
        {
            "type": "request",
            "seq": 1,
            "command": "runInTerminal",
            "arguments": {"title": "日本語 😀"},
        },
        {
            "type": "response",
            "seq": 2,
            "request_seq": 7,
            "command": "stackTrace",
            "success": False,
            "message": "α",
            "body": {"error": {"id": 3}},
        },
        {
            "type": "event",
            "seq": 3,
            "event": "stopped",
            "body": {"reason": "breakpoint"},
        },
    ]
    process = process_with_output(b"".join(frame(value) for value in values))
    connection = Connection(process)
    messages = [message async for message in connection.messages()]
    assert messages == [
        DapRequest(1, "runInTerminal", {"title": "日本語 😀"}),
        DapResponse(2, 7, "stackTrace", False, {"error": {"id": 3}}, "α"),
        DapEvent(3, "stopped", {"reason": "breakpoint"}),
    ]
    process.wait.assert_awaited_once()
    with pytest.raises(ToolCommandError, match="already have a reader"):
        await anext(connection.messages())


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"type": "event", "seq": True, "event": "stopped"},
        {"type": "event", "seq": -1, "event": "stopped"},
        {"type": "event", "seq": 1, "event": ""},
        {"type": "request", "seq": 1, "command": None},
        {"type": "unknown", "seq": 1},
        {
            "type": "response",
            "seq": 1,
            "request_seq": True,
            "command": "launch",
            "success": True,
        },
        {
            "type": "response",
            "seq": 1,
            "request_seq": 2,
            "command": "launch",
            "success": 1,
        },
        {
            "type": "response",
            "seq": 1,
            "request_seq": 2,
            "command": "launch",
            "success": True,
            "message": None,
        },
    ],
)
def test_invalid_envelopes_raise_domain_errors(value):
    """Reject malformed envelope identities without interpreting operation payloads."""
    with pytest.raises(ToolParserError):
        parse_message(value)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "wire",
    [
        b"Content-Length: wrong\r\n\r\n{}",
        b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        b"malformed\r\n\r\n{}",
        b"Content-Length: 8\r\n\r\n{}",
        b"Content-Length: 2\r\n\r\n\xff\xff",
        b"Content-Length: 3\r\n\r\nNaN",
        frame({"seq": 1, "type": "event"}),
    ],
)
async def test_invalid_or_incomplete_wire_is_not_successful_eof(wire):
    """Surface malformed headers, JSON, UTF-8, and partial frames as parser failures."""
    connection = Connection(process_with_output(wire))
    with pytest.raises(ToolParserError):
        _ = [message async for message in connection.messages()]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "wire",
    [
        b"Content-Length: 99999\r\n\r\n",
        b"x" * 65,
    ],
)
async def test_declared_body_and_unfinished_header_have_byte_limits(wire, monkeypatch):
    """Reject oversized frames before retaining their advertised body."""
    monkeypatch.setattr(Connection, "MAX_HEADER_BYTES", 64)
    monkeypatch.setattr(Connection, "MAX_MESSAGE_BYTES", 256)
    with pytest.raises(ToolParserError, match="byte limit"):
        _ = [
            message
            async for message in Connection(process_with_output(wire)).messages()
        ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "message,expected",
    [
        (
            DapRequest(1, "launch", {"program": "α.exe"}),
            {
                "seq": 1,
                "type": "request",
                "command": "launch",
                "arguments": {"program": "α.exe"},
            },
        ),
        (
            DapResponse(2, 1, "runInTerminal", False, message="Unsupported"),
            {
                "seq": 2,
                "type": "response",
                "request_seq": 1,
                "command": "runInTerminal",
                "success": False,
                "message": "Unsupported",
            },
        ),
        (
            DapEvent(3, "initialized"),
            {"seq": 3, "type": "event", "event": "initialized"},
        ),
    ],
)
async def test_send_serializes_envelopes_with_byte_lengths(message, expected):
    """Queue exact complete frames while omitting absent optional envelope fields."""
    process = process_with_output(b"")
    connection = Connection(process)
    await connection.send(message)
    header, body = process.write_stdin.call_args.args[0].encode("latin_1").split(b"\r\n\r\n")
    assert header == f"Content-Length: {len(body)}".encode("ascii")
    assert json.loads(body) == expected
    await connection.close()
    process.close.assert_awaited_once()


@pytest.mark.anyio
async def test_send_bounds_payload_and_rejects_nonfinite_json(monkeypatch):
    """Fail unsupported JSON and oversized output before writing stdin."""
    process = process_with_output(b"")
    connection = Connection(process)
    with pytest.raises(ToolParserError, match="serialize"):
        await connection.send(DapRequest(1, "test", {"number": float("nan")}))
    monkeypatch.setattr(Connection, "MAX_MESSAGE_BYTES", 32)
    with pytest.raises(ToolParserError, match="byte limit"):
        await connection.send(DapRequest(1, "test", {"value": "a" * 100}))
    process.write_stdin.assert_not_awaited()


@pytest.mark.anyio
async def test_process_failures_retain_process_provenance():
    """Distinguish adapter exit and stream failures from clean protocol EOF."""
    with pytest.raises(ToolCommandError) as exited:
        _ = [message async for message in Connection(process_with_output(b"", code=7)).messages()]
    assert exited.value.process_id == 17

    async def failed_output() -> AsyncGenerator[ProcessOutput]:
        """Report a retained process stream failure rather than clean EOF."""
        yield ProcessOutput("stderr", "diagnostic before failure")
        raise ProcessStreamError("read failed")

    process = process_with_output(b"")
    process.output = failed_output
    with pytest.raises(ToolCommandError) as failed:
        _ = [message async for message in Connection(process).messages()]
    assert failed.value.process_id == 17
    process.write_stdin.side_effect = ProcessStreamError("write failed")
    with pytest.raises(ToolCommandError) as written:
        await Connection(process).send(DapRequest(1, "initialize"))
    assert written.value.process_id == 17


@pytest.mark.anyio
async def test_connect_preserves_environment_and_closes_owned_process_on_cancel(
    cpp_acceptance_project: Path,
):
    """Keep the adapter alive without idle deadlines and close its context on cancellation."""
    process = process_with_output(b"")

    @asynccontextmanager
    async def running_process() -> AsyncGenerator[SimpleNamespace]:
        """Represent ProcessSession context ownership without launching an executable."""
        try:
            yield process
        finally:
            await process.close()

    processes = SimpleNamespace(launch=AsyncMock(return_value=running_process()))
    executable = cpp_acceptance_project / "lldb-dap.exe"
    environment = {"DAP_TEST_ENVIRONMENT": "configured"}
    # The explicit connect contract intentionally mirrors the clangd process test.
    # pylint: disable=duplicate-code
    spec = create_spec(
        executable,
        processes,
        environment=environment,
        inherit_environment=False,
    )
    methods = cast(Methods, spec.methods)
    started = asyncio.Event()

    async def owner() -> None:
        """Hold one connection open until the test cancels its lifetime owner."""
        async with methods["connect"](cpp_acceptance_project) as connection:
            assert connection.process_id == 17
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    processes.launch.assert_awaited_once_with(
        executable.resolve(),
        (),
        cwd=cpp_acceptance_project,
        env=environment,
        inherit_environment=False,
        encoding=ProcessEncoding(driver="latin_1", errors="strict"),
        timeout=ProcessTimeout(),
    )
    process.close.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["version", "connect"])
async def test_adapter_launch_failure_does_not_expose_native_or_private_data(
    cpp_acceptance_project,
    operation,
):
    """Explain operator recovery without copying a failed process launch's private details."""
    processes = SimpleNamespace(
        launch=AsyncMock(side_effect=ProcessStartError("PRIVATE_NATIVE_PATH_AND_ENVIRONMENT")),
    )
    methods = create_spec(cpp_acceptance_project / "adapter", processes).methods
    with pytest.raises(ToolCommandError) as failure:
        if operation == "version":
            await methods["version"]()
        else:
            async with methods["connect"](cpp_acceptance_project):
                pytest.fail("A failed process must not yield a connection.")
    text = str(failure.value)
    assert "operator" in text and "toolset" in text
    assert "PRIVATE" not in text and failure.value.process_id is None


@pytest.mark.anyio
@pytest.mark.parametrize("code", [0, 7])
async def test_version_failure_retains_launched_process_id(cpp_acceptance_project, code):
    """Keep provenance for both unrecognized banners and nonzero probe exits."""
    process = process_with_output(b"", code=code)

    @asynccontextmanager
    async def running_process():
        """Own the scripted version-probe process through completion."""
        yield process

    processes = SimpleNamespace(launch=AsyncMock(return_value=running_process()))
    methods = create_spec(cpp_acceptance_project / "adapter", processes).methods
    with pytest.raises(ToolCommandError) as failure:
        await methods["version"]()
    assert failure.value.process_id == 17
    if code == 0:
        assert "4096 characters" in str(failure.value)
        assert "process_get(process_id=17)" in str(failure.value)
    else:
        assert "exit 7" in str(failure.value)
