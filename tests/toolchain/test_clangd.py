"""LSP byte framing stays inside the clangd tool specification."""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from forgemcp.process.models import ProcessEncoding, ProcessTimeout
from forgemcp.toolchain.errors import ToolParserError
from forgemcp.toolchain.tools.clangd import (
    Connection,
    LspNotification,
    LspResponse,
    Methods,
    create_spec,
)


@pytest.fixture
def anyio_backend():
    """Run async tests on the asyncio backend used by the process and LSP services."""
    return "asyncio"


@pytest.mark.anyio
async def test_connect_limits_background_index_workers(
    cpp_acceptance_project: Path,
) -> None:
    """Keep background indexing enabled while limiting each launched clangd to two workers."""
    database = cpp_acceptance_project / "compile_commands.json"
    database.write_text("[]", encoding="utf-8")
    process = SimpleNamespace(process_id=17)

    @asynccontextmanager
    async def running_process() -> AsyncGenerator[SimpleNamespace]:
        """Supply a process session without launching an external program."""
        yield process

    processes = SimpleNamespace(launch=AsyncMock(return_value=running_process()))
    executable = cpp_acceptance_project / "clangd.exe"
    environment = {"CLANGD_TEST_ENVIRONMENT": "configured"}
    spec = create_spec(
        executable,
        processes,
        environment=environment,
        inherit_environment=False,
    )
    methods = cast(Methods, spec.methods)
    async with methods["connect"](
        cpp_acceptance_project,
        cpp_acceptance_project,
    ) as connection:
        assert connection.process_id == process.process_id

    processes.launch.assert_awaited_once_with(
        executable.resolve(),
        (
            f"--compile-commands-dir={cpp_acceptance_project}",
            "--background-index",
            "-j=2",
        ),
        cwd=cpp_acceptance_project,
        env=environment,
        inherit_environment=False,
        encoding=ProcessEncoding(driver="latin_1", errors="strict"),
        timeout=ProcessTimeout(),
    )


@pytest.mark.anyio
async def test_fragmented_utf8_frames_and_serialization_use_byte_lengths():
    """Verify fragmented UTF-8 LSP frames and outgoing Content-Length count bytes correctly."""
    values = [
        {"jsonrpc": "2.0", "id": 7, "result": "日本語 😀"},
        {"jsonrpc": "2.0", "method": "note", "params": {"text": "α"}},
    ]
    wire = b""
    for value in values:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        wire += f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body

    async def output() -> AsyncGenerator[SimpleNamespace]:
        """Yield scripted fragmented stdout and unrelated stderr chunks."""
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
    """Verify invalid headers, envelopes, and incomplete frames raise tool parser errors."""
    async def output() -> AsyncGenerator[SimpleNamespace]:
        """Yield the parametrized invalid byte frame through lossless Latin-1 transport."""
        yield SimpleNamespace(stream="stdout", text=wire.decode("latin_1"))

    process = SimpleNamespace(process_id=1, output=output, wait=AsyncMock(return_value=0))
    with pytest.raises(ToolParserError):
        _ = [message async for message in Connection(process).messages()]
