"""Process lifecycle, stream encoding, timeout, and read-only MCP contract tests."""

import asyncio
import json
import os
import sys
import shutil
from pathlib import Path

import pytest

from forgemcp.process.service import ProcessService, ChunkDecoder, ProcessRecord
from forgemcp.process.models import ProcessEncoding, ProcessTimeout
from forgemcp.process.errors import (
    ProcessError,
    ProcessExitedError,
    ProcessStreamError,
    ProcessStartError,
)


@pytest.fixture
def anyio_backend():
    """Run async tests on the asyncio backend used by the process and LSP services."""
    return "asyncio"


@pytest.fixture
async def processes(cpp_acceptance_project):
    """Yield an isolated process service and close every session within a bounded test lifetime."""
    service = ProcessService(cpp_acceptance_project)
    async with asyncio.timeout(15):
        try:
            yield service
        finally:
            await service.close()


@pytest.mark.anyio
@pytest.mark.parametrize("inherit", [True, False])
async def test_environment_modes(processes, monkeypatch, inherit):
    """Verify child variables are explicit and parent inheritance follows the requested mode."""
    monkeypatch.setenv("FORGEMCP_PARENT_TEST", "parent")
    async with await processes.launch(
        sys.executable,
        ("-c", "import os,json; print(json.dumps(dict(os.environ)))"),
        env={"FORGEMCP_CHILD_TEST": "child"},
        inherit_environment=inherit,
    ) as session:
        output = "".join([chunk.text async for chunk in session.output()])
        assert await session.wait() == 0
    environment = json.loads(output)
    assert environment["FORGEMCP_CHILD_TEST"] == "child"
    assert ("FORGEMCP_PARENT_TEST" in environment) == inherit


@pytest.mark.anyio
async def test_arguments_are_literal_and_retained(processes, cpp_acceptance_project):
    """Verify shell-like arguments reach the process literally and remain in its summary."""
    values = [
        "hello world",
        "a&b",
        "(x)",
        "",
        "a|b",
        "a>b",
        'quote" & literal',
        "%FORGEMCP_SHELL_TEST%",
        "bang!",
        "caret^",
    ]
    arguments = ("-c", "import json,sys; print(json.dumps(sys.argv[1:]))", *values)
    async with await processes.launch(sys.executable, arguments) as session:
        output = "".join([chunk.text async for chunk in session.output()])
        assert await session.wait() == 0
        summary = processes.records[session.process_id].summary
        assert summary.arguments == list(arguments)
        assert summary.executable == sys.executable
        assert summary.cwd == str(cpp_acceptance_project.resolve())
    assert json.loads(output) == values


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows executable paths")
async def test_executable_path_is_literal(processes, cpp_acceptance_project):
    """Verify Windows executable paths containing shell characters launch literally."""
    folder = cpp_acceptance_project / "Tools %FORGEMCP_SHELL_TEST% & more"
    folder.mkdir()
    executable = folder / "python.exe"
    shutil.copy2(sys.executable, executable)
    (folder / "pyvenv.cfg").write_text(
        (Path(sys.executable).parent.parent / "pyvenv.cfg").read_text()
    )
    async with await processes.launch(executable, ("-c", "print(123)")) as session:
        assert (
            "".join(
                [
                    item.text
                    async for item in session.output()
                    if item.stream == "stdout"
                ]
            ).strip()
            == "123"
        )
        assert await session.wait() == 0


@pytest.mark.anyio
async def test_stdin_fifo_eof_both_outputs_and_repeat_wait(processes):
    """Verify ordered stdin, idempotent EOF and wait, and single-consumer output behavior."""
    code = "import sys; data=sys.stdin.read(); print(data,end=''); print('err',file=sys.stderr)"
    async with await processes.launch(sys.executable, ("-c", code)) as session:
        await session.write_stdin("first")
        await session.write_stdin("second")
        await session.close_stdin()
        await session.close_stdin()
        with pytest.raises(ProcessExitedError):
            await session.write_stdin("late")
        output = {"stdout": "", "stderr": ""}
        async for chunk in session.output():
            output[chunk.stream] += chunk.text
        assert output == {"stdout": "firstsecond", "stderr": "err" + os.linesep}
        assert await session.wait() == await session.wait() == session.returncode == 0
        with pytest.raises(ProcessStreamError):
            await anext(session.output())
    record = processes.records[session.process_id]
    assert record.status.current_status == 0
    assert (
        "".join(
            entry.text for entry in record.transcript if entry.stream == "stdin"
        )
        == "firstsecond"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("send_eof", [False, True])
async def test_stdin_cleanup_after_child_exit(processes, send_eof):
    async with await processes.launch(
        sys.executable,
        ("-c", "print('finished')"),
    ) as session:
        if send_eof:
            await session.close_stdin()
        assert await session.wait() == 0
        assert session.failure is None
        writer = session.process.stdin
        assert writer.is_closing()
        await writer.wait_closed()


@pytest.mark.anyio
async def test_shutdown_with_buffered_stdin_does_not_hang(processes):
    session = await processes.launch(
        sys.executable,
        ("-c", "import time; print('ready', flush=True); time.sleep(30)"),
    )
    try:
        assert (await anext(session.output())).text.strip() == "ready"
        await session.write_stdin("x" * (4 * 1024 * 1024))
        async with asyncio.timeout(5):
            while not session.process.stdin.transport.get_write_buffer_size():
                await asyncio.sleep(0)
            await session.close()
        assert session.task.done()
        assert session.returncode is not None
        # Terminating a child with unread input may report a broken pipe; the
        # cleanup must still finish instead of waiting forever for stdin drain.
        assert session.failure is None or isinstance(session.failure, ProcessStreamError)
    finally:
        await session.close()


@pytest.mark.anyio
async def test_wait_drains_large_output_without_consumer(processes):
    """Verify waiting drains large stdout and stderr without a concurrent output consumer."""
    code = "import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000); sys.exit(7)"
    async with await processes.launch(sys.executable, ("-c", code)) as session:
        assert await session.wait() == 7
        output = {"stdout": "", "stderr": ""}
        async for chunk in session.output():
            output[chunk.stream] += chunk.text
        assert output == {"stdout": "x" * 200000, "stderr": "y" * 200000}


@pytest.mark.anyio
async def test_cancel_wait_does_not_stop_session(processes):
    """Verify cancelling a waiter leaves the process and its stream workers alive."""
    async with await processes.launch(
        sys.executable,
        ("-c", "import sys; sys.stdin.read()"),
    ) as session:
        waiter = asyncio.create_task(session.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert session.returncode is None
        assert not session.task.done()
        await session.close_stdin()
        assert await session.wait() == 0


@pytest.mark.anyio
async def test_context_cancellation_stops_workers(processes):
    """Verify cancellation of the context owner stops the process and all stream workers."""
    ready = asyncio.Event()
    sessions = []

    async def owner():
        """Own a long-running session until the test cancels its context task."""
        async with await processes.launch(
            sys.executable,
            ("-c", "import time; time.sleep(30)"),
        ) as session:
            sessions.append(session)
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(owner())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sessions[0].returncode is not None
    assert sessions[0].task.done()
    await sessions[0].close()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["total", "idle"])
async def test_timeout_reaches_output_and_wait(processes, mode):
    """Verify total and idle timeouts surface through both output and wait after cleanup."""
    session = await processes.launch(
        sys.executable,
        ("-c", "import time; time.sleep(30)"),
        timeout=ProcessTimeout(**{mode: 0.1}),
    )
    with pytest.raises(ProcessError):
        async with session:
            async for _ in session.output():
                pass
    with pytest.raises(ProcessError):
        await session.wait()
    assert session.returncode is not None
    assert session.task.done()
    assert processes.records[session.process_id].status.current_status == "interrupted"
    snapshot = processes.records[session.process_id].snapshot()
    assert snapshot.status.current_status == "interrupted"
    assert not hasattr(snapshot.status, "transcript")


@pytest.mark.anyio
async def test_decode_failure_is_not_eof(processes):
    """Verify strict decoding failures remain process errors rather than successful stream EOF."""
    with pytest.raises(ProcessStreamError):
        async with await processes.launch(
            sys.executable,
            ("-c", "import sys; sys.stdout.buffer.write(bytes([255]))"),
            encoding=ProcessEncoding("utf-8", errors="strict"),
        ) as session:
            async for _ in session.output():
                pass
    assert session.returncode is not None
    assert processes.records[session.process_id].status.current_status == "stream_failure"
    with pytest.raises(ProcessStreamError):
        await session.wait()


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["plain", "nested", "multiple"])
@pytest.mark.usefixtures("processes")
async def test_worker_failure_preserves_single_exception_or_multiple_group(
    request,
    monkeypatch,
    kind,
):
    """Verify worker failures unwrap singleton groups but preserve groups with multiple errors."""
    service = request.getfixturevalue("processes")
    first = ProcessStreamError("first stream failure")
    expected = first
    failure = first
    if kind == "nested":
        failure = ExceptionGroup(
            "outer",
            [ExceptionGroup("inner", [first])],
        )
    elif kind == "multiple":
        expected = ExceptionGroup(
            "multiple stream failures",
            [first, ProcessStreamError("second stream failure")],
        )
        failure = expected
    read_stream = ProcessRecord.read_stream

    async def fail_stdout(record, stream, reader, decoder, queue):
        """Inject the selected stdout failure while keeping the stderr worker unchanged."""
        if stream == "stdout":
            raise failure
        await read_stream(record, stream, reader, decoder, queue)

    monkeypatch.setattr(ProcessRecord, "read_stream", fail_stdout)
    with pytest.raises(type(expected)) as caught:
        async with await service.launch(
            sys.executable,
            ("-c", "import time; time.sleep(30)"),
        ) as session:
            await session.wait()
    assert caught.value is expected
    assert session.failure is expected
    assert session.returncode is not None
    assert session.task.done()
    assert session.record.status.current_status == "stream_failure"
    with pytest.raises(type(expected)) as repeated:
        await session.wait()
    assert repeated.value is expected


def test_incremental_decoder_and_latin1():
    """Verify fragmented multibyte decoding and lossless Latin-1 byte transport."""
    decoder = ChunkDecoder(ProcessEncoding("utf-8", errors="strict"))
    data = "日本語".encode()
    assert (
        "".join(decoder.decode(bytes([byte])) for byte in data) + decoder.final()
        == "日本語"
    )
    decoder = ChunkDecoder(ProcessEncoding("latin_1", errors="strict"))
    assert decoder.decode(bytes(range(256))).encode("latin_1") == bytes(range(256))


@pytest.mark.anyio
async def test_service_close_stops_all_sessions(processes):
    """Verify service shutdown terminates every retained session."""
    sessions = [
        await processes.launch(sys.executable, ("-c", "import time; time.sleep(30)"))
        for _ in range(2)
    ]
    await processes.close()
    await processes.close()
    assert all(
        session.returncode is not None and session.task.done() for session in sessions
    )


@pytest.mark.anyio
async def test_invalid_launch_has_no_records(processes, cpp_acceptance_project):
    """Verify invalid executable, codec, or working-directory inputs create no process records."""
    for kwargs in (
        {"cwd": cpp_acceptance_project.parent},
        {"encoding": ProcessEncoding("invalid-codec")},
    ):
        with pytest.raises(ProcessStartError):
            await processes.launch(sys.executable, **kwargs)
    with pytest.raises(ProcessStartError):
        await processes.launch(cpp_acceptance_project / "missing-program")
    assert not processes.records


@pytest.mark.anyio
async def test_configured_storage_is_an_allowed_working_directory(
    cpp_acceptance_project,
):
    """Verify the configured storage root is allowed and unrelated working directories are
    rejected.
    """
    from forgemcp.workspace.service import WorkspaceService

    workspace = WorkspaceService(
        cpp_acceptance_project / "src",
        cpp_acceptance_project / "process-storage",
    )
    directory = workspace.storage_root / "build"
    directory.mkdir(parents=True)
    service = ProcessService(workspace.root, allowed_roots=(workspace.storage_root,))
    try:
        async with await service.launch(
            sys.executable,
            ("-c", "print('storage')"),
            cwd=directory,
        ) as session:
            assert await session.wait() == 0
        with pytest.raises(ProcessStartError):
            await service.launch(sys.executable, cwd=workspace.root.parent)
    finally:
        await service.close()


@pytest.mark.anyio
async def test_cancel_output_does_not_close_session(processes):
    """Verify cancelling an output consumer does not terminate its process session."""
    async with await processes.launch(
        sys.executable,
        ("-c", "import sys; sys.stdin.read()"),
    ) as session:
        reader = asyncio.create_task(anext(session.output()))
        await asyncio.sleep(0)
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert session.returncode is None
        await session.close_stdin()
        assert await session.wait() == 0


@pytest.mark.anyio
async def test_launch_cancellation_delegates_to_asyncio(processes, monkeypatch):
    """Verify launch cancellation propagates through the injected asyncio subprocess operation."""
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def launch(*args, **kwargs):
        """Signal that subprocess creation began and block until cancelled."""
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    task = asyncio.create_task(processes.launch(sys.executable))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert not processes.records


@pytest.mark.anyio
@pytest.mark.parametrize("interval", [0, 1])
async def test_process_overview_uses_shared_progress(processes, monkeypatch, interval):
    """Verify overview progress follows the shared per-invocation throttle."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from mcp.server import MCPServer
    from mcp.server.apps import Apps
    from forgemcp.completion import Complete

    processes.progress_interval = interval
    monkeypatch.setattr("forgemcp.progress.monotonic", lambda: 0)
    async with await processes.launch(sys.executable, ["-c", "pass"]) as session:
        await session.wait()
    apps = Apps()
    processes.register(MCPServer("test", extensions=[apps]), apps, Complete())
    handler = apps.tools()[0].fn
    for _ in range(2):
        ctx = SimpleNamespace(report_progress=AsyncMock())
        result = await handler(ctx=ctx)
        assert result.structured_content["completed"] == 1
        values = [call.args[0] for call in ctx.report_progress.await_args_list]
        assert values == ([0, 1] if interval == 0 else [0])


@pytest.mark.anyio
async def test_process_inspection_tools_and_markdown_resources(processes):
    """Verify read-only process tools, resources, metadata, progress, and ID completions."""
    from mcp import Client
    from mcp.server import MCPServer
    from mcp.server.apps import Apps
    from forgemcp.completion import Complete

    code = (
        "import sys; "
        "sys.stdout.write('\\x1b[31mred\\x1b[0m'); sys.stdout.flush(); "
        "sys.stderr.write('warning\\n'); sys.stderr.flush()"
    )
    async with await processes.launch(sys.executable, ("-c", code)) as session:
        await session.wait()
    apps = Apps()
    server = MCPServer("test", extensions=[apps])
    processes.register(server, apps, Complete())
    for tool in apps.tools():
        server.add_tool(tool.fn, meta=tool.meta, **tool.kwargs)
    for resource in apps.resources():
        server.add_resource(resource.resource)
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert {"processes_overview", "process_get"} <= tools.keys()
        assert all(
            tools[name].icons and tools[name].meta["ui"]["resourceUri"]
            for name in ("processes_overview", "process_get")
        )
        overview = await client.call_tool("processes_overview", {})
        assert "Completed successfully" in overview.content[0].text
        item = overview.structured_content["processes"][0]
        assert item["summary"]["process_id"] == session.process_id
        assert item["status"]["current_status"] == 0
        assert "outcome" not in item
        assert "transcript" not in item["status"]
        details = await client.call_tool("process_get", {"process_id": session.process_id})
        assert not details.is_error and details.content
        assert "stdout — " in details.content[0].text and "stderr — " in details.content[0].text
        assert "Returned transcript lines" in details.content[0].text
        result = details.structured_content
        assert result["process"] == item
        assert result["transcript"]
        assert [
            (entry["stream"], entry["text"]) for entry in result["transcript"]
        ] == [
            (entry.stream, entry.text)
            for entry in processes.records[session.process_id].transcript
        ]
        assert all("elapsed_seconds" not in entry for entry in result["transcript"])
        assert "\x1b[31mred\x1b[0m" in "".join(
            entry["text"] for entry in result["transcript"] if entry["stream"] == "stdout"
        )
        overview_md = (
            await client.read_resource(processes.OVERVIEW_URI)
        ).contents[0].text
        details_md = (
            await client.read_resource(f"forgemcp://processes/{session.process_id}")
        ).contents[0].text
        assert "Completed successfully" in overview_md
        assert "Completed successfully" in details_md
        assert "UTC" in details_md
        assert " · +" in details_md
        assert " s" in details_md
        assert "### stdout" in details_md and "### stderr" in details_md
        assert (await client.call_tool("process_get", {"process_id": 99999})).is_error


@pytest.mark.anyio
async def test_process_snapshot_distinguishes_running_and_nonzero_exit(processes):
    """Verify snapshots distinguish running state from a completed nonzero exit."""
    async with await processes.launch(
        sys.executable,
        ("-c", "import sys; sys.stdin.read(); sys.exit(7)"),
    ) as session:
        running = processes.records[session.process_id].snapshot()
        assert running.status.current_status == "running"
        await session.close_stdin()
        assert await session.wait() == 7
    completed = processes.records[session.process_id].snapshot()
    assert completed.status.current_status == 7
