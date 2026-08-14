"""State, handle, event, and lifecycle tests using a deterministic DAP subprocess."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.debugger.errors import DebuggerHandleExpiredError, DebuggerRequestError, DebuggerStaleDataError, DebuggerUnsupportedError
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
        send("response", request_seq=sequence_id, command=command, success=True, body={"supportsCancelRequest": True, "supportsConfigurationDoneRequest": True})
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

    def __init__(self, runtime: ProcessRuntime, adapter_script: str = _FAKE_ADAPTER) -> None:
        self.runtime = runtime
        self.adapter_script = adapter_script

    def discover(self) -> DebugAdapterInfo:
        return DebugAdapterInfo(backend_id=self.backend_id, display_name="Fake DAP", available=True, supported_modes=("launch",))

    async def start_adapter(self):
        return await self.runtime.start((sys.executable, "-u", "-c", self.adapter_script))

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


def test_configuration_capability_is_validated_and_false_skips_configuration_done(tmp_path: Path):
    no_configuration_done = _FAKE_ADAPTER.replace(
        'body={"supportsCancelRequest": True, "supportsConfigurationDoneRequest": True}',
        'body={"supportsCancelRequest": True}',
    ).replace(
        'elif command == "launch":\n        launch_sequence = sequence_id\n        send("event", event="initialized", body={})',
        'elif command == "launch":\n        send("event", event="initialized", body={})\n        send("response", request_seq=sequence_id, command=command, success=True, body={})',
    )
    invalid_capability = _FAKE_ADAPTER.replace(
        'body={"supportsCancelRequest": True, "supportsConfigurationDoneRequest": True}',
        'body={"supportsConfigurationDoneRequest": "yes"}',
    )

    async def exercise() -> None:
        source = tmp_path / "source.cpp"
        source.write_text("int main() {}\n", encoding="utf-8")
        build = tmp_path / "build"
        build.mkdir()
        (build / "demo.exe").write_bytes(b"placeholder")
        config = ForgeConfig(workspace_root=tmp_path)
        runtime = ProcessRuntime(config, create_logger("CRITICAL"), policy=ProcessPolicy(allowed_executables=frozenset(), allowed_executable_paths=frozenset({Path(sys.executable).resolve()})))
        workspace = WorkspaceService(config, create_logger("CRITICAL"))
        service = DebuggerService(workspace, runtime, _FakeBackend(runtime, no_configuration_done))  # type: ignore[arg-type]
        try:
            status = await service.launch(DebugLaunchRequest(program="build/demo.exe", cwd="build"))
            assert status.state is DebuggerState.RUNNING
            assert "supportsConfigurationDoneRequest" not in status.capabilities
            await service.stop()

            invalid = DebuggerService(workspace, runtime, _FakeBackend(runtime, invalid_capability))  # type: ignore[arg-type]
            with pytest.raises(DebuggerRequestError, match="invalid capability"):
                await invalid.launch(DebugLaunchRequest(program="build/demo.exe", cwd="build"))
            assert (await invalid.status()).state is DebuggerState.TERMINATED
            assert runtime._handles == set()
        finally:
            await service.aclose()
            await runtime.aclose()

    asyncio.run(exercise())


def test_stop_preempts_hung_configuration_and_retains_one_terminal_event(tmp_path: Path):
    hung_configuration = _FAKE_ADAPTER.replace(
        'elif command == "configurationDone":\n        send("response", request_seq=sequence_id, command=command, success=True, body={})\n        send("response", request_seq=launch_sequence, command="launch", success=True, body={})\n        send("event", event="stopped", body={"reason": "breakpoint", "threadId": 7, "description": "fake-stop"})\n        send("event", event="output", body={"category": "console", "output": "fake output"})',
        'elif command == "configurationDone":\n        import time\n        time.sleep(30)',
    )

    async def exercise() -> None:
        source = tmp_path / "source.cpp"
        source.write_text("int main() {}\n", encoding="utf-8")
        build = tmp_path / "build"
        build.mkdir()
        (build / "demo.exe").write_bytes(b"placeholder")
        config = ForgeConfig(workspace_root=tmp_path)
        runtime = ProcessRuntime(config, create_logger("CRITICAL"), policy=ProcessPolicy(allowed_executables=frozenset(), allowed_executable_paths=frozenset({Path(sys.executable).resolve()})))
        service = DebuggerService(WorkspaceService(config, create_logger("CRITICAL")), runtime, _FakeBackend(runtime, hung_configuration))  # type: ignore[arg-type]
        launching = asyncio.create_task(service.launch(DebugLaunchRequest(program="build/demo.exe", cwd="build")))
        try:
            for _ in range(100):
                if (await service.status()).state is DebuggerState.CONFIGURING:
                    break
                await asyncio.sleep(0.01)
            assert (await service.status()).state is DebuggerState.CONFIGURING
            stopped = await asyncio.wait_for(service.stop(), timeout=5.0)
            assert stopped.state is DebuggerState.TERMINATED
            with pytest.raises(asyncio.CancelledError):
                await launching
            events = await service.events(limit=256)
            assert [event.kind for event in events.events].count("terminated") == 1
            assert runtime._handles == set()
            await service.aclose()
            assert (await service.events(limit=256)).events == ()
        finally:
            if not launching.done():
                launching.cancel()
                await asyncio.gather(launching, return_exceptions=True)
            await runtime.aclose()

    asyncio.run(exercise())


def test_read_responses_become_stale_on_concurrent_continue_and_evaluate_grammar_is_minimal(tmp_path: Path):
    slow_threads = _FAKE_ADAPTER.replace("import sys", "import sys\nimport time").replace(
        'elif command == "threads":', 'elif command == "threads":\n        time.sleep(0.2)'
    )

    async def exercise() -> None:
        source = tmp_path / "source.cpp"
        source.write_text("int main() {}\n", encoding="utf-8")
        build = tmp_path / "build"
        build.mkdir()
        (build / "demo.exe").write_bytes(b"placeholder")
        config = ForgeConfig(workspace_root=tmp_path)
        runtime = ProcessRuntime(config, create_logger("CRITICAL"), policy=ProcessPolicy(allowed_executables=frozenset(), allowed_executable_paths=frozenset({Path(sys.executable).resolve()})))
        service = DebuggerService(WorkspaceService(config, create_logger("CRITICAL")), runtime, _FakeBackend(runtime, slow_threads))  # type: ignore[arg-type]
        try:
            await service.launch(DebugLaunchRequest(program="build/demo.exe", cwd="build"))
            await asyncio.sleep(0.03)
            threads = await service.threads()
            delayed_read = asyncio.create_task(service.threads())
            await asyncio.sleep(0.02)
            await service.continue_execution(threads[0].thread_id)
            with pytest.raises(DebuggerStaleDataError):
                await delayed_read
            for expression in ("value = 1", "value += 1", "++value", "call()", "arr[index()]", "value;", "value\nnext", "`help`", "value/*comment*/", "(int)value", "new T", "value[0]", "object.member", "\u00a0value"):
                with pytest.raises(DebuggerUnsupportedError):
                    await service.evaluate("not-a-valid-frame", expression)
        finally:
            await service.stop()
            await runtime.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("paused", (False, True))
def test_adapter_crash_from_running_or_paused_reaches_failed_without_live_runtime_handles(tmp_path: Path, paused: bool):
    stopped = '        send("event", event="stopped", body={"reason": "breakpoint", "threadId": 7})\n' if paused else ""
    crashing_configuration = _FAKE_ADAPTER.replace(
        'elif command == "configurationDone":\n        send("response", request_seq=sequence_id, command=command, success=True, body={})\n        send("response", request_seq=launch_sequence, command="launch", success=True, body={})\n        send("event", event="stopped", body={"reason": "breakpoint", "threadId": 7, "description": "fake-stop"})\n        send("event", event="output", body={"category": "console", "output": "fake output"})',
        'elif command == "configurationDone":\n'
        '        send("response", request_seq=sequence_id, command=command, success=True, body={})\n'
        '        send("response", request_seq=launch_sequence, command="launch", success=True, body={})\n'
        + stopped
        + '        import time\n        time.sleep(0.1)\n        break',
    )

    async def exercise() -> None:
        source = tmp_path / "source.cpp"
        source.write_text("int main() {}\n", encoding="utf-8")
        build = tmp_path / "build"
        build.mkdir()
        (build / "demo.exe").write_bytes(b"placeholder")
        config = ForgeConfig(workspace_root=tmp_path)
        runtime = ProcessRuntime(config, create_logger("CRITICAL"), policy=ProcessPolicy(allowed_executables=frozenset(), allowed_executable_paths=frozenset({Path(sys.executable).resolve()})))
        service = DebuggerService(WorkspaceService(config, create_logger("CRITICAL")), runtime, _FakeBackend(runtime, crashing_configuration))  # type: ignore[arg-type]
        try:
            launched = await service.launch(DebugLaunchRequest(program="build/demo.exe", cwd="build"))
            assert launched.state in ({DebuggerState.RUNNING, DebuggerState.PAUSED} if paused else {DebuggerState.RUNNING})
            for _ in range(100):
                if (await service.status()).state is DebuggerState.FAILED:
                    break
                await asyncio.sleep(0.01)
            assert (await service.status()).state is DebuggerState.FAILED
            assert runtime._handles == set()
        finally:
            await service.aclose()
            await runtime.aclose()

    asyncio.run(exercise())


def test_duplicate_terminal_events_are_deduplicated_while_stop_epochs_stay_conservative(tmp_path: Path):
    async def exercise() -> None:
        config = ForgeConfig(workspace_root=tmp_path)
        runtime = ProcessRuntime(config, create_logger("CRITICAL"), policy=ProcessPolicy(allowed_executables=frozenset(), allowed_executable_paths=frozenset({Path(sys.executable).resolve()})))
        service = DebuggerService(WorkspaceService(config, create_logger("CRITICAL")), runtime, _FakeBackend(runtime))  # type: ignore[arg-type]
        try:
            await service._begin_launch()
            await service._on_event("stopped", {"reason": "pause", "threadId": 1})
            first_generation = (await service.status()).stop_generation
            await service._on_event("stopped", {"reason": "pause", "threadId": 1})
            assert (await service.status()).stop_generation == first_generation + 1
            await service._on_event("continued", {})
            await service._on_event("continued", {})
            await service._on_event("terminated", {})
            await service._on_event("terminated", {})
            page = await service.events(limit=256)
            assert [event.kind for event in page.events].count("terminated") == 1
            assert (await service.status()).state is DebuggerState.TERMINATED
        finally:
            await service.aclose()
            await runtime.aclose()

    asyncio.run(exercise())
