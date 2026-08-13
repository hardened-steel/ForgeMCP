"""State, handle, event, and lifecycle tests using a deterministic DAP subprocess."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.debugger.errors import DebuggerHandleExpiredError
from forgemcp.debugger.models import DebugAdapterInfo, DebugBreakpointSpec, DebugLaunchRequest, DebuggerState
from forgemcp.debugger.plugin import _LaunchArguments
from forgemcp.debugger.service import DebuggerService
from forgemcp.processes import ProcessPolicy, ProcessRuntime
from forgemcp.workspace import WorkspaceService


_FAKE_ADAPTER = r'''
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
        name, value = line.decode("ascii").split(":", 1)
        headers[name.casefold()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])).decode("utf-8"))

sequence = 1
source_path = ""
launch_sequence = None
def send(kind, **value):
    global sequence
    body = {"seq": sequence, "type": kind, **value}
    sequence += 1
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(b"Content-Length: " + str(len(data)).encode("ascii") + b"\r\n\r\n" + data)
    sys.stdout.buffer.flush()

while True:
    request = receive()
    if request is None:
        break
    command = request["command"]
    arguments = request.get("arguments", {})
    sequence_id = request["seq"]
    if command == "initialize":
        send("response", request_seq=sequence_id, command=command, success=True, body={"supportsCancelRequest": True})
    elif command == "launch":
        launch_sequence = sequence_id
        send("event", event="initialized", body={})
    elif command == "setBreakpoints":
        source_path = arguments["source"]["path"]
        send("response", request_seq=sequence_id, command=command, success=True, body={"breakpoints": [{"id": 41, "verified": True, "line": 3, "column": 1}]})
    elif command == "configurationDone":
        send("response", request_seq=sequence_id, command=command, success=True, body={})
        send("response", request_seq=launch_sequence, command="launch", success=True, body={})
        send("event", event="stopped", body={"reason": "breakpoint", "threadId": 7, "description": "fake-stop"})
        send("event", event="output", body={"category": "console", "output": "fake output"})
    elif command == "threads":
        send("response", request_seq=sequence_id, command=command, success=True, body={"threads": [{"id": 7, "name": "main"}]})
    elif command == "stackTrace":
        send("response", request_seq=sequence_id, command=command, success=True, body={"stackFrames": [{"id": 17, "name": "main", "source": {"path": source_path, "name": "source.cpp"}, "line": 3, "column": 1}]})
    elif command == "scopes":
        send("response", request_seq=sequence_id, command=command, success=True, body={"scopes": [{"name": "Locals", "variablesReference": 99, "expensive": False}]})
    elif command == "variables":
        send("response", request_seq=sequence_id, command=command, success=True, body={"variables": [{"name": "value", "value": "42", "type": "int", "variablesReference": 0}]})
    elif command == "evaluate":
        send("response", request_seq=sequence_id, command=command, success=True, body={"result": "42", "type": "int", "variablesReference": 0})
    elif command in {"continue", "next", "stepIn", "stepOut"}:
        send("response", request_seq=sequence_id, command=command, success=True, body={})
        send("event", event="continued", body={})
    elif command == "pause":
        send("response", request_seq=sequence_id, command=command, success=True, body={})
        send("event", event="stopped", body={"reason": "pause", "threadId": 7})
    elif command == "disconnect":
        send("response", request_seq=sequence_id, command=command, success=True, body={})
        break
    elif command == "cancel":
        send("response", request_seq=sequence_id, command=command, success=True, body={})
'''


class _FakeBackend:
    backend_id = "fake-dap"

    def __init__(self, runtime: ProcessRuntime) -> None:
        self.runtime = runtime

    def discover(self) -> DebugAdapterInfo:
        return DebugAdapterInfo(backend_id=self.backend_id, display_name="Fake DAP", available=True, supported_modes=("launch",))

    async def start_adapter(self):
        return await self.runtime.start((sys.executable, "-u", "-c", _FAKE_ADAPTER))

    def initialize_arguments(self):
        return {"clientID": "test", "adapterID": "fake", "linesStartAt1": True, "columnsStartAt1": True}

    def launch_arguments(self, *, program: str, cwd: str, args, environment, stop_on_entry: bool):
        return {"program": program, "cwd": cwd, "args": list(args), "env": dict(environment), "stopOnEntry": stop_on_entry, "console": "internalConsole"}


def test_fake_adapter_drives_configuration_handles_events_and_stop_generation(tmp_path: Path):
    async def exercise() -> None:
        source = tmp_path / "source.cpp"
        source.write_text("int main() { int value = 42; return value; }\n", encoding="utf-8")
        build = tmp_path / "build"
        build.mkdir()
        (build / "demo.exe").write_bytes(b"placeholder")
        config = ForgeConfig(workspace_root=tmp_path)
        runtime = ProcessRuntime(config, create_logger("CRITICAL"), policy=ProcessPolicy(allowed_executables=frozenset(), allowed_executable_paths=frozenset({Path(sys.executable).resolve()})))
        service = DebuggerService(WorkspaceService(config, create_logger("CRITICAL")), runtime, _FakeBackend(runtime))  # type: ignore[arg-type]

        status = await service.launch(DebugLaunchRequest(program="build/demo.exe", cwd="build", initial_breakpoints={"source.cpp": (DebugBreakpointSpec(line=2, column=0),)}))
        await asyncio.sleep(0.02)
        assert (await service.status()).state is DebuggerState.PAUSED
        assert status.session_generation == 1
        threads = await service.threads()
        assert len(threads) == 1
        frames = await service.stack_trace(threads[0].thread_id)
        assert frames[0].line == 2
        assert frames[0].source is not None and frames[0].source.path == "source.cpp"
        scopes = await service.scopes(frames[0].frame_id)
        variables = await service.variables(scopes[0].variables_id or "")
        assert variables[0].value == "42"
        assert (await service.evaluate(frames[0].frame_id, "value")).result == "42"
        page = await service.events(limit=10)
        assert [event.kind for event in page.events] == ["stopped", "output"]

        await service.continue_execution(threads[0].thread_id)
        await asyncio.sleep(0.02)
        assert (await service.status()).state is DebuggerState.RUNNING
        await service.pause()
        await asyncio.sleep(0.02)
        assert (await service.status()).state is DebuggerState.PAUSED
        with pytest.raises(DebuggerHandleExpiredError):
            await service.scopes(frames[0].frame_id)
        stopped_page = await service.events(after_sequence=page.next_cursor, limit=10)
        assert [event.kind for event in stopped_page.events] == ["continued", "stopped"]
        assert (await service.stop()).state is DebuggerState.TERMINATED
        await runtime.aclose()

    asyncio.run(exercise())


def test_debugger_launch_schema_preserves_empty_default_factories_for_mcp_clients():
    schema = _LaunchArguments.model_json_schema()
    properties = schema["properties"]
    assert {"args", "environment", "initial_breakpoints", "stop_on_entry"} <= properties.keys()
    request = _LaunchArguments.model_validate({"program": "build/demo.exe"})
    assert request.args == []
    assert request.environment == {}
    assert request.initial_breakpoints == {}
    assert request.stop_on_entry is True
