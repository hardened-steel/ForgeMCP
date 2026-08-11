"""Behaviour tests for the safe asyncio external-process runtime."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.processes import (
    ProcessArgumentError,
    ProcessEnvironmentError,
    ProcessExecutableError,
    ProcessPolicy,
    ProcessRuntime,
    ProcessWorkingDirectoryError,
)


def runtime(root: Path, **policy_values: object) -> ProcessRuntime:
    """Compose a test runtime that authorizes only this test interpreter."""
    policy = ProcessPolicy(
        allowed_executables=frozenset(),
        allowed_executable_paths=frozenset({Path(sys.executable).resolve()}),
        default_timeout_seconds=1.0,
        maximum_timeout_seconds=5.0,
        termination_grace_seconds=0.2,
        stream_close_timeout_seconds=0.1,
        **policy_values,
    )
    return ProcessRuntime(ForgeConfig(workspace_root=root), create_logger("CRITICAL"), policy=policy)


def test_run_returns_unicode_stdout_stderr_and_nonzero_exit_code(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)
        result = await service.run(
            [
                sys.executable,
                "-c",
                "import sys; print('žąsinas'); print('klaida', file=sys.stderr); raise SystemExit(7)",
            ]
        )

        assert result.exit_code == 7
        assert result.timed_out is False
        assert result.stdout.text == f"žąsinas{os.linesep}"
        assert result.stdout.truncated is False
        assert result.stderr.text == f"klaida{os.linesep}"
        assert result.stderr.truncated is False
        await service.aclose()

    asyncio.run(exercise())


def test_run_bounds_each_stream_independently(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path, max_output_characters=5)
        result = await service.run(
            [
                sys.executable,
                "-c",
                "import sys; print('abcdef'); print('uvwxyz', file=sys.stderr)",
            ]
        )

        assert result.exit_code == 0
        assert result.stdout.text == "abcde"
        assert result.stdout.truncated is True
        assert result.stderr.text == "uvwxy"
        assert result.stderr.truncated is True
        await service.aclose()

    asyncio.run(exercise())


def test_run_timeout_terminates_the_child_and_returns_timeout_model(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)
        result = await service.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout_seconds=0.05)

        assert result.timed_out is True
        assert result.exit_code is None
        await service.aclose()

    asyncio.run(exercise())


def test_cancelling_run_cleans_up_its_child(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)
        operation = asyncio.create_task(
            service.run([sys.executable, "-c", "import time; time.sleep(30)"])
        )
        await asyncio.sleep(0.05)
        operation.cancel()

        with pytest.raises(asyncio.CancelledError):
            await operation
        assert service._handles == set()
        await service.aclose()

    asyncio.run(exercise())


def test_rejects_shell_strings_and_cwd_outside_the_workspace(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)
        with pytest.raises(ProcessArgumentError, match="argv sequence"):
            await service.run(f'{sys.executable} -c "print(1)"')  # type: ignore[arg-type]
        with pytest.raises(ProcessWorkingDirectoryError):
            await service.run([sys.executable, "-c", "print(1)"], cwd="../outside")
        await service.aclose()

    asyncio.run(exercise())


def test_bare_name_allow_list_does_not_authorize_an_absolute_path_with_the_same_name(tmp_path):
    async def exercise() -> None:
        service = ProcessRuntime(
            ForgeConfig(workspace_root=tmp_path),
            create_logger("CRITICAL"),
            policy=ProcessPolicy(allowed_executables=frozenset({Path(sys.executable).stem})),
        )
        with pytest.raises(ProcessExecutableError, match="not allowed"):
            await service.run([sys.executable, "-c", "print(1)"])
        await service.aclose()

    asyncio.run(exercise())


def test_environment_inheritance_and_overrides_are_policy_controlled(tmp_path):
    async def exercise() -> None:
        locked_service = runtime(tmp_path)
        with pytest.raises(ProcessEnvironmentError, match="not allowed"):
            await locked_service.run(
                [sys.executable, "-c", "print(1)"], environment={"FORGEMCP_TEST_VALUE": "blocked"}
            )
        await locked_service.aclose()

        service = runtime(
            tmp_path,
            allow_environment_inheritance=False,
            allowed_environment_overrides=frozenset({"FORGEMCP_TEST_VALUE"}),
        )
        result = await service.run(
            [sys.executable, "-c", "import os; print(os.environ['FORGEMCP_TEST_VALUE'])"],
            environment={"FORGEMCP_TEST_VALUE": "safe-value"},
            inherit_environment=False,
        )

        assert result.stdout.text == f"safe-value{os.linesep}"
        await service.aclose()

    asyncio.run(exercise())


def test_streaming_handle_supports_stdin_stdout_and_shutdown_without_leaking_processes(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)
        handle = await service.start(
            [
                sys.executable,
                "-u",
                "-c",
                "import sys; line = sys.stdin.readline(); print(line, end=''); sys.stdout.flush()",
            ]
        )

        handle.stdin.write("message\n".encode())
        await handle.stdin.drain()
        assert (await handle.stdout.readline()).rstrip(b"\r\n") == b"message"
        assert await handle.wait(timeout_seconds=1.0) == 0

        sleeping_handle = await service.start([sys.executable, "-c", "import time; time.sleep(30)"])
        await service.aclose()
        assert sleeping_handle.returncode is not None
        assert service._handles == set()

    asyncio.run(exercise())


def test_runtime_shutdown_terminates_a_non_detached_child_process(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)
        marker = tmp_path / "escaped-child.txt"
        child_code = (
            "import pathlib, time; "
            f"time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
        )
        parent_code = (
            "import subprocess, sys, time; "
            f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            "print(child.pid, flush=True); time.sleep(30)"
        )
        handle = await service.start([sys.executable, "-u", "-c", parent_code])
        assert (await handle.stdout.readline()).strip().isdigit()

        await service.aclose()
        await asyncio.sleep(0.7)
        assert not marker.exists()

    asyncio.run(exercise())
