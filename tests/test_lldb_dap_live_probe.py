"""Opt-in Phase-0 live DAP probe, deliberately kept out of production code."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.processes import ProcessPolicy, ProcessRuntime

_LIVE_ADAPTER_VARIABLE = "FORGEMCP_LLDB_DAP_LIVE_TEST"
_MAX_DAP_MESSAGE_BYTES = 1_048_576


async def _read_test_only_dap_message(reader: asyncio.StreamReader) -> dict[str, object]:
    """Read one bounded DAP message for the opt-in integration gate only."""
    header_block = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
    if len(header_block) > 8_192:
        raise AssertionError("DAP adapter sent an oversized header block during the live probe.")
    headers: dict[str, str] = {}
    for line in header_block[:-4].split(b"\r\n"):
        name, separator, value = line.partition(b":")
        if not separator:
            raise AssertionError("DAP adapter sent a malformed header during the live probe.")
        headers[name.decode("ascii").casefold()] = value.decode("ascii").strip()
    length = int(headers["content-length"])
    if not 0 <= length <= _MAX_DAP_MESSAGE_BYTES:
        raise AssertionError("DAP adapter sent an oversized message during the live probe.")
    payload = await asyncio.wait_for(reader.readexactly(length), timeout=5.0)
    parsed = json.loads(payload.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError("DAP adapter sent a non-object message during the live probe.")
    return parsed


async def _test_only_dap_request(
    handle,
    *,
    sequence: int,
    command: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    """Frame one fixed test request and return its matching response.

    This is intentionally test-local: it validates the admission gate without
    introducing a production DAP transport ahead of Phase 1.
    """
    payload = json.dumps(
        {"seq": sequence, "type": "request", "command": command, "arguments": arguments},
        separators=(",", ":"),
    ).encode("utf-8")
    handle.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload)
    await handle.stdin.drain()
    while True:
        message = await _read_test_only_dap_message(handle.stdout)
        if message.get("type") == "response" and message.get("request_seq") == sequence:
            return message


@pytest.mark.skipif(
    not os.environ.get(_LIVE_ADAPTER_VARIABLE),
    reason="opt-in real lldb-dap gate requires FORGEMCP_LLDB_DAP_LIVE_TEST",
)
def test_live_lldb_dap_initialize_disconnect_uses_the_strict_runtime(tmp_path: Path):
    """Prove the configured standalone adapter speaks minimal DAP then leaves no handle."""

    async def exercise() -> None:
        executable = Path(os.environ[_LIVE_ADAPTER_VARIABLE])
        policy = ProcessPolicy(
            allowed_executables=frozenset(),
            allowed_executable_paths=frozenset({executable}),
            allow_environment_inheritance=False,
            default_timeout_seconds=10.0,
            maximum_timeout_seconds=10.0,
        )
        runtime = ProcessRuntime(
            ForgeConfig(workspace_root=tmp_path),
            create_logger("CRITICAL"),
            policy=policy,
        )
        handle = await runtime.start_trusted_adapter(
            [str(executable)], approved_path_directories=(executable.parent,)
        )
        try:
            initialize = await _test_only_dap_request(
                handle,
                sequence=1,
                command="initialize",
                arguments={
                    "clientID": "forgemcp-phase0-test",
                    "adapterID": "lldb",
                    "pathFormat": "path",
                    "linesStartAt1": True,
                    "columnsStartAt1": True,
                    "supportsRunInTerminalRequest": False,
                },
            )
            assert initialize.get("success") is True
            disconnect = await _test_only_dap_request(
                handle,
                sequence=2,
                command="disconnect",
                arguments={"terminateDebuggee": False},
            )
            assert disconnect.get("success") is True
        finally:
            await handle.aclose()
            await runtime.aclose()
        assert handle.returncode is not None
        assert runtime._handles == set()

    asyncio.run(exercise())
