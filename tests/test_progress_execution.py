"""Regression coverage for request-scoped progress and bounded observation."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from forgemcp.cmake.progress import CMakeOutputProgressObserver
from forgemcp.cmake.service import CMakeService
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.models import ProcessOutput, ProcessResult
from forgemcp.plugins import (
    ProgressUpdate,
    ToolExecutionContext,
    invoke_tool_handler,
)
from forgemcp.processes import (
    ProcessOutputEvent,
    ProcessPolicy,
    ProcessRuntime,
)
from forgemcp.workspace import WorkspaceService
from forgemcp.server import _McpProgressReporter


class RecordingReporter:
    def __init__(self, *, fail: bool = False) -> None:
        self.updates: list[ProgressUpdate] = []
        self.fail = fail

    @property
    def supports_progress(self) -> bool:
        return True

    async def report(self, update: ProgressUpdate) -> None:
        if self.fail:
            raise RuntimeError("observer transport is unavailable")
        self.updates.append(update)


def _runtime(root: Path) -> ProcessRuntime:
    return ProcessRuntime(
        ForgeConfig(workspace_root=root),
        create_logger("CRITICAL"),
        policy=ProcessPolicy(
            allowed_executables=frozenset(),
            allowed_executable_paths=frozenset({Path(sys.executable).resolve()}),
            default_timeout_seconds=2.0,
            maximum_timeout_seconds=5.0,
            termination_grace_seconds=0.2,
            stream_close_timeout_seconds=0.1,
        ),
    )


def test_legacy_and_context_tool_handlers_are_isolated_and_backward_compatible():
    context = ToolExecutionContext(RecordingReporter())
    old_calls: list[dict[str, object]] = []
    context_calls: list[ToolExecutionContext] = []
    legacy_contexts: list[object | None] = []

    def legacy(arguments: dict[str, object]) -> dict[str, object]:
        old_calls.append(arguments)
        return {"legacy": True}

    def modern(arguments: dict[str, object], *, execution_context: ToolExecutionContext) -> dict[str, object]:
        context_calls.append(execution_context)
        return {"modern": arguments["value"]}

    def legacy_with_optional_context(arguments: dict[str, object], context: object | None = None) -> dict[str, object]:
        legacy_contexts.append(context)
        return {"legacy_context": arguments["value"]}

    assert invoke_tool_handler(legacy, {"value": 1}, context) == {"legacy": True}
    assert invoke_tool_handler(modern, {"value": 2}, context) == {"modern": 2}
    assert invoke_tool_handler(legacy_with_optional_context, {"value": 3}, context) == {"legacy_context": 3}
    assert old_calls == [{"value": 1}]
    assert context_calls == [context]
    assert legacy_contexts == [None]


def test_progress_message_validation_and_failing_reporter_do_not_change_workflow():
    reporter = RecordingReporter(fail=True)
    context = ToolExecutionContext(reporter)

    async def exercise() -> None:
        await context.report_progress(ProgressUpdate(0, None, "Preparing build"))

    asyncio.run(exercise())
    assert reporter.updates == []
    try:
        ProgressUpdate(1, 1, r"C:\secret\build")
    except ValueError:
        pass
    else:  # pragma: no cover - explicit disclosure guard
        raise AssertionError("absolute host paths must not be accepted as progress labels")
    try:
        ProgressUpdate(1, 1, "password=not-for-progress")
    except ValueError:
        pass
    else:  # pragma: no cover - explicit disclosure guard
        raise AssertionError("secret-bearing progress labels must not be accepted")


def test_mcp_reporter_accepts_numeric_zero_and_forces_terminal_past_rate_limit():
    class ContextShell:
        def __init__(self) -> None:
            self.request_context = SimpleNamespace(meta=SimpleNamespace(progressToken=0))
            self.calls: list[tuple[float, float | None, str]] = []

        async def report_progress(self, progress: float, total: float | None, message: str) -> None:
            self.calls.append((progress, total, message))

    async def exercise() -> ContextShell:
        shell = ContextShell()
        reporter = _McpProgressReporter(shell)  # type: ignore[arg-type]
        assert reporter.supports_progress is True
        await reporter.report(ProgressUpdate(0, None, "Preparing build"))
        await reporter.report(ProgressUpdate(0, None, "Build started"))
        await reporter.report(ProgressUpdate(0, None, "Build completed", terminal=True, completed=True))
        return shell

    shell = asyncio.run(exercise())
    assert shell.calls == [
        (0.0, None, "Preparing build"),
        (0.0, None, "Build completed"),
    ]


def test_strict_ninja_ctest_and_adversarial_output_progress_parsing():
    reporter = RecordingReporter()
    context = ToolExecutionContext(reporter)
    now = datetime.now(UTC)

    async def exercise() -> None:
        build = CMakeOutputProgressObserver(context, "build")
        await build(ProcessOutputEvent("stdout", "[2/10] CXX object\n", now))
        await build(ProcessOutputEvent("stderr", "[1/10] stale interleaving\n", now))
        await build(ProcessOutputEvent("stderr", "warning: 9/10 diagnostic\n[11/10] nope\n", now))
        test_context = ToolExecutionContext(reporter)
        test = CMakeOutputProgressObserver(test_context, "test")
        await test(ProcessOutputEvent("stdout", " 1/3 Test #1: žąsinas ...   Passed  0.00 sec\n", now))
        await test(ProcessOutputEvent("stdout", " 9999999/99999999 Test #1: C:/secret ... Passed\n", now))

    asyncio.run(exercise())
    exact = [(item.progress, item.total) for item in reporter.updates if item.total is not None]
    assert exact == [(2.0, 10.0), (1.0, 3.0)]
    assert all("žąsinas" not in item.message and "secret" not in item.message for item in reporter.updates)


def test_process_observer_is_bounded_and_does_not_block_stdout_stderr_draining(tmp_path: Path):
    observed: list[ProcessOutputEvent] = []

    async def slow_observer(event: ProcessOutputEvent) -> None:
        observed.append(event)
        await asyncio.sleep(0.05)

    async def exercise() -> None:
        runtime = _runtime(tmp_path)
        # Multiple megabytes force many pipe reads while ProcessResult itself
        # remains under its pre-existing capture bounds.
        code = (
            "import os, sys; data=b'x'*65536; "
            "[os.write(sys.stdout.fileno(), data) for _ in range(40)]; "
            "[os.write(sys.stderr.fileno(), data) for _ in range(40)]"
        )
        result = await asyncio.wait_for(
            runtime.run([sys.executable, "-c", code], observer=slow_observer), timeout=4.0
        )
        assert result.exit_code == 0
        assert result.stdout.truncated is True and result.stderr.truncated is True
        assert result.observer_overflow is True
        assert result.observer_failed is False
        assert result.duration_milliseconds >= 0
        assert observed
        await runtime.aclose()

    asyncio.run(exercise())


def test_process_observer_failure_is_isolated_from_the_process_result(tmp_path: Path):
    async def failing_observer(_: ProcessOutputEvent) -> None:
        raise RuntimeError("observer only")

    async def exercise() -> None:
        runtime = _runtime(tmp_path)
        result = await runtime.run([sys.executable, "-c", "print('safe')"], observer=failing_observer)
        assert result.exit_code == 0
        assert result.observer_failed is True
        await runtime.aclose()

    asyncio.run(exercise())


def test_process_observer_decodes_split_utf8_and_finalizes_no_newline(tmp_path: Path):
    reporter = RecordingReporter()
    context = ToolExecutionContext(reporter)
    observer = CMakeOutputProgressObserver(context, "test")

    async def exercise() -> None:
        runtime = _runtime(tmp_path)
        code = (
            "import os, sys, time; "
            "os.write(sys.stdout.fileno(), b' 1/2 Test #1: \\xc5'); "
            "time.sleep(0.03); os.write(sys.stdout.fileno(), b'\\xbe\\xc4\\x85sinas ... Passed')"
        )
        result = await runtime.run([sys.executable, "-c", code], observer=observer)
        assert result.exit_code == 0
        await runtime.aclose()

    asyncio.run(exercise())
    assert any(item.message == "Test completed" for item in reporter.updates)
    assert all("žąsinas" not in item.message for item in reporter.updates)


def test_cancellation_suppressing_observer_cannot_extend_process_result(tmp_path: Path):
    released = asyncio.Event()

    async def suppress_cancel(_: ProcessOutputEvent) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await released.wait()

    async def exercise() -> None:
        runtime = _runtime(tmp_path)
        result = await asyncio.wait_for(
            runtime.run([sys.executable, "-c", "print('bounded')"], observer=suppress_cancel),
            timeout=1.0,
        )
        assert result.exit_code == 0
        assert result.observer_failed is True
        released.set()
        await asyncio.sleep(0)
        await runtime.aclose()

    asyncio.run(exercise())


def test_silent_cmake_operation_uses_a_bounded_elapsed_heartbeat(tmp_path: Path):
    class SlowRunner:
        async def run(self, argv, *, cwd=".", timeout_seconds=None, observer=None) -> ProcessResult:
            await asyncio.sleep(2.05)
            now = datetime.now(UTC)
            return ProcessResult(
                exit_code=0,
                started_at=now,
                finished_at=now,
                stdout=ProcessOutput(text=""),
                stderr=ProcessOutput(text=""),
            )

    reporter = RecordingReporter()
    context = ToolExecutionContext(reporter)
    service = CMakeService(
        WorkspaceService(ForgeConfig(workspace_root=tmp_path), create_logger("CRITICAL")), SlowRunner()
    )

    async def exercise() -> None:
        result = await service.configure(binary_dir="build", execution_context=context)
        assert result.process.exit_code == 0
        assert "file_api_unavailable" in result.warnings

    asyncio.run(exercise())
    assert any("Configure running (2s)" == item.message for item in reporter.updates)
    assert reporter.updates[-1].message == "Configure completed with warnings"


def test_context_progress_is_monotonic_across_phase_heartbeat_exact_and_terminal_success():
    reporter = RecordingReporter()
    context = ToolExecutionContext(reporter)

    async def exercise() -> None:
        await context.report_progress(ProgressUpdate(0, None, "Preparing build"))
        await context.report_progress(ProgressUpdate(1, 10, "Building"))
        await context.report_progress(ProgressUpdate(0, None, "Build running (2s)"))
        await context.report_progress(ProgressUpdate(0, None, "Build completed", terminal=True, completed=True))

    asyncio.run(exercise())
    assert [(item.progress, item.total) for item in reporter.updates] == [
        (0.0, None), (1.0, 10.0), (1.0, 10.0), (10.0, 10.0),
    ]


def test_progress_parser_flushes_eof_line_rejects_oversized_suffix_and_disables_reset_counters():
    reporter = RecordingReporter()
    context = ToolExecutionContext(reporter)
    now = datetime.now(UTC)

    async def exercise() -> None:
        build = CMakeOutputProgressObserver(context, "build")
        await build(ProcessOutputEvent("stdout", "x" * 4_096, now))
        await build(ProcessOutputEvent("stdout", "x" * 4_096, now))
        await build(ProcessOutputEvent("stdout", "x" * 1_000 + "[2/10] CXX object\n", now))
        await build(ProcessOutputEvent("stdout", "[2/10] CXX object", now))
        await build.aclose()
        await build(ProcessOutputEvent("stdout", "[1/10] reset\n", now))
        await build(ProcessOutputEvent("stdout", "[3/10] CXX object\n", now))
        await context.report_progress(build.terminal_success_update("Build completed"))

    asyncio.run(exercise())
    assert [(item.progress, item.total) for item in reporter.updates] == [
        (2.0, 10.0), (10.0, 10.0),
    ]
    assert reporter.updates[-1].completed is True


def test_parser_defers_exact_completion_so_failure_never_publishes_false_100_percent():
    reporter = RecordingReporter()
    context = ToolExecutionContext(reporter)
    observer = CMakeOutputProgressObserver(context, "build")
    now = datetime.now(UTC)

    async def exercise() -> None:
        await observer(ProcessOutputEvent("stdout", "[1/2] Building CXX object\n", now))
        await observer(ProcessOutputEvent("stdout", "[2/2] Linking CXX executable\n", now))
        await context.report_progress(ProgressUpdate(0, None, "Build failed", terminal=True))

    asyncio.run(exercise())
    assert [(item.progress, item.total, item.message) for item in reporter.updates] == [
        (1.0, 2.0, "Building"),
        (1.0, 2.0, "Build failed"),
    ]
