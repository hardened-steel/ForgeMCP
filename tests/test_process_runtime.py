"""Behaviour tests for the safe asyncio external-process runtime."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.processes import (
    ProcessArgumentError,
    ProcessEnvironmentError,
    ProcessExecutableError,
    ProcessOwnershipError,
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


def test_run_feeds_bounded_opaque_stdin_and_closes_it(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path, max_input_bytes=4)
        result = await service.run(
            [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().hex())"],
            input_data=b"\x00\xffab",
        )
        assert result.exit_code == 0
        assert result.stdout.text.strip() == "00ff6162"

        with pytest.raises(ProcessArgumentError, match="input exceeds"):
            await service.run(
                [sys.executable, "-c", "pass"], input_data=b"12345"
            )
        assert service._handles == set()
        await service.aclose()

    asyncio.run(exercise())


def test_run_without_input_closes_stdin_instead_of_leaving_a_reader_blocked(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)
        result = await service.run(
            [sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"]
        )
        assert result.stdout.text.strip() == "0"
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


def test_default_runtime_tolerates_an_unavailable_explicit_clangd_path(tmp_path):
    configured = tmp_path / "missing-clangd.exe"
    service = ProcessRuntime(
        ForgeConfig(workspace_root=tmp_path, clangd_path=configured), create_logger("CRITICAL")
    )

    assert configured.resolve(strict=False) not in service.policy.allowed_executable_paths
    asyncio.run(service.aclose())


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


def test_completed_handle_cleanup_is_idempotent_and_does_not_resignal_its_old_group(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)
        handle = await service.start([sys.executable, "-c", "pass"])

        assert await handle.wait(timeout_seconds=1.0) == 0
        await handle.terminate()
        await handle.kill()
        await handle.aclose()

        assert service._handles == set()
        await service.aclose()

    asyncio.run(exercise())


@pytest.mark.skipif(os.name == "nt", reason="Windows closes the Job Object when its adapter exits")
def test_wait_reaps_a_posix_adapter_grandchild_after_its_parent_exits(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)
        marker = tmp_path / "grandchild-after-parent-exit.txt"
        child_code = (
            "import pathlib, time; "
            f"time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
        )
        parent_code = (
            "import subprocess, sys; "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}])"
        )
        handle = await service.start([sys.executable, "-c", parent_code])

        assert await handle.wait(timeout_seconds=1.0) == 0
        await asyncio.sleep(0.7)
        assert not marker.exists()
        assert service._handles == set()
        await service.aclose()

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


def test_exact_approval_rejects_path_spoofing_and_detects_replacement(tmp_path):
    async def exercise() -> None:
        executable = Path(sys.executable).resolve()
        service = runtime(tmp_path)
        spoof_directory = tmp_path / "spoof"
        spoof_directory.mkdir()
        spoof = spoof_directory / executable.name
        spoof.write_text("not an executable", encoding="utf-8")
        service._executable_search_path = str(spoof_directory)

        with pytest.raises(ProcessExecutableError, match="not allowed"):
            await service.run([executable.name, "--version"])

        assert service.policy.approves_exact_executable(executable) is True
        await service.aclose()

        approved_copy = tmp_path / executable.name
        shutil.copy2(executable, approved_copy)
        approval_policy = ProcessPolicy(
            allowed_executables=frozenset(),
            allowed_executable_paths=frozenset({approved_copy}),
        )
        assert approval_policy.approves_exact_executable(approved_copy) is True
        approved_copy.write_bytes(b"replacement")
        assert approval_policy.approves_exact_executable(approved_copy) is False

    asyncio.run(exercise())


def test_exact_approval_rejects_a_symlinked_executable(tmp_path):
    target = Path(sys.executable).resolve()
    link = tmp_path / target.name
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("the host does not permit test symlink creation")

    with pytest.raises(ValueError, match="symlinks or reparse points"):
        ProcessPolicy(allowed_executable_paths=frozenset({link}))


def test_exact_approval_rejects_a_reparse_point_path(tmp_path, monkeypatch):
    import forgemcp.processes.policy as policy_module

    target = Path(sys.executable).resolve()
    monkeypatch.setattr(policy_module, "_is_reparse_point", lambda path: path == target)

    with pytest.raises(ValueError, match="symlinks or reparse points"):
        ProcessPolicy(allowed_executable_paths=frozenset({target}))


@pytest.mark.skipif(os.name != "nt", reason="Windows executable paths are case-insensitive")
def test_exact_approval_uses_case_insensitive_windows_path_comparison(tmp_path):
    target = Path(sys.executable).resolve()
    policy = ProcessPolicy(allowed_executables=frozenset(), allowed_executable_paths=frozenset({target}))

    assert policy.approves_exact_executable(Path(str(target).swapcase())) is True


def test_trusted_adapter_environment_is_scrubbed_and_path_is_explicit(tmp_path, monkeypatch):
    async def exercise() -> None:
        monkeypatch.setenv("FORGEMCP_TEST_SECRET", "must-not-reach-adapter")
        service = runtime(tmp_path)
        companion = tmp_path / "companion"
        companion.mkdir()
        result = await service.run_trusted_adapter(
            [
                sys.executable,
                "-c",
                "import json, os; print(json.dumps({'secret': os.getenv('FORGEMCP_TEST_SECRET'), 'path': os.environ.get('PATH'), 'system_root': bool(os.environ.get('SystemRoot'))}))",
            ],
            approved_path_directories=(companion,),
        )

        observed = json.loads(result.stdout.text)
        assert observed["secret"] is None
        assert observed["path"].split(os.pathsep) == [str(Path(sys.executable).resolve().parent), str(companion.resolve())]
        if os.name == "nt":
            assert observed["system_root"] is True
        await service.aclose()

    asyncio.run(exercise())


def test_normal_process_callers_keep_inheritance_and_best_effort_mode(tmp_path, monkeypatch):
    async def exercise() -> None:
        monkeypatch.setenv("FORGEMCP_TEST_NORMAL", "still-inherited")
        service = runtime(tmp_path)
        handle = await service.start(
            [sys.executable, "-u", "-c", "import os; print(os.environ['FORGEMCP_TEST_NORMAL'], flush=True)"]
        )

        assert handle.required_ownership is False
        assert handle.environment_mode.value == "inherit"
        assert (await handle.stdout.readline()).decode().strip() == "still-inherited"
        assert await handle.wait(timeout_seconds=1.0) == 0
        await service.aclose()

    asyncio.run(exercise())


def test_trusted_adapter_termination_and_idempotent_close_kill_its_grandchild(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)
        marker = tmp_path / "trusted-grandchild.txt"
        child_code = (
            "import pathlib, time; "
            f"time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
        )
        parent_code = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            "time.sleep(30)"
        )
        handle = await service.start_trusted_adapter([sys.executable, "-c", parent_code])

        assert handle.required_ownership is True
        assert handle.ownership_established is True
        await handle.aclose()
        await handle.aclose()
        await asyncio.sleep(0.7)
        assert not marker.exists()
        await service.aclose()

    asyncio.run(exercise())


@pytest.mark.skipif(os.name == "nt", reason="Windows uses the Job Object path")
def test_posix_required_ownership_escalates_from_term_to_kill_for_the_whole_group(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)
        marker = tmp_path / "term-ignoring-grandchild.txt"
        child_code = (
            "import pathlib, signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
        )
        parent_code = (
            "import subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(30)"
        )
        handle = await service.start_trusted_adapter([sys.executable, "-c", parent_code])

        await handle.terminate()
        await asyncio.sleep(0.7)
        assert not marker.exists()
        await service.aclose()

    asyncio.run(exercise())


def test_trusted_adapter_timeout_and_cancellation_leave_no_process_tree(tmp_path):
    async def exercise() -> None:
        service = runtime(tmp_path)

        def command(marker: Path) -> list[str]:
            child_code = (
                "import pathlib, time; "
                f"time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('escaped', encoding='utf-8')"
            )
            parent_code = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); time.sleep(30)"
            )
            return [sys.executable, "-c", parent_code]

        timeout_marker = tmp_path / "timeout-grandchild.txt"
        timed_out = await service.run_trusted_adapter(command(timeout_marker), timeout_seconds=0.05)
        assert timed_out.timed_out is True

        cancellation_marker = tmp_path / "cancelled-grandchild.txt"
        task = asyncio.create_task(service.run_trusted_adapter(command(cancellation_marker)))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await asyncio.sleep(0.7)
        assert not timeout_marker.exists()
        assert not cancellation_marker.exists()
        assert service._handles == set()
        await service.aclose()

    asyncio.run(exercise())


@pytest.mark.skipif(os.name != "nt", reason="required Job Object assignment is Windows-specific")
def test_required_windows_ownership_assignment_failure_reaps_the_unreturned_child(tmp_path, monkeypatch):
    import forgemcp.processes.runtime as runtime_module

    class _FailedJob:
        def attach(self, process) -> bool:
            return False

        def close(self) -> None:
            return None

    async def exercise() -> None:
        service = runtime(tmp_path)
        marker = tmp_path / "assignment-failure.txt"
        monkeypatch.setattr(runtime_module._WindowsProcessJob, "create", classmethod(lambda cls: _FailedJob()))
        with pytest.raises(ProcessOwnershipError):
            await service.start_trusted_adapter(
                [
                    sys.executable,
                    "-c",
                    f"import pathlib, time; time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text('started')",
                ]
            )
        await asyncio.sleep(0.7)
        assert not marker.exists()
        assert service._handles == set()
        await service.aclose()

    asyncio.run(exercise())
