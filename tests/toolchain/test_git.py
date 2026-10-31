"""Literal Git execution, byte transport, environment isolation, and process cleanup."""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forgemcp.process.errors import ProcessStartError
from forgemcp.process.models import ProcessTimeout
from forgemcp.toolchain.errors import ToolCommandError
from forgemcp.toolchain.tools import git


class FragmentedSession:
    """Emit byte-sized tagged chunks and expose process cleanup and input."""

    process_id = 37

    def __init__(self, stdout: bytes, stderr: bytes = b"", code: int = 0) -> None:
        """Retain streams without decoding to model byte-framed Git output."""
        self.stdout = stdout
        self.stderr = stderr
        self.code = code
        self.closed = False
        self.write_stdin = AsyncMock()
        self.close_stdin = AsyncMock()

    async def __aenter__(self):
        """Enter the sole owned execution context."""
        return self

    async def __aexit__(self, *_args):
        """Record cleanup even when consumption fails."""
        self.closed = True

    async def output(self):
        """Split UTF-8 sequences and NUL records across arbitrary stream chunks."""
        for stream, value in (("stdout", self.stdout), ("stderr", self.stderr)):
            for byte in value:
                yield SimpleNamespace(stream=stream, text=bytes((byte,)).decode("latin_1"))

    async def wait(self):
        """Return the command-specific exit status without raising."""
        return self.code


@pytest.mark.anyio
async def test_run_preserves_fragmented_bytes_and_literal_arguments(cpp_acceptance_project):
    """Keep arbitrary bytes, NUL names, Unicode input, and option-like paths unchanged."""
    output = b"entry\0" + "α.cpp".encode("utf-8") + b"\0\xff\x00"
    session = FragmentedSession(output, "warning: α".encode("utf-8"), 1)
    launch = AsyncMock(return_value=session)
    methods = git.create_spec(
        cpp_acceptance_project / "git",
        SimpleNamespace(launch=launch),
        {"HOME": "operator-home"},
        False,
    ).methods
    result = await methods["run"](
        cpp_acceptance_project,
        ["status", "--porcelain=v2", "-z", "--", "-path[1].cpp"],
        stdin="message α\n",
        read_only=True,
    )
    assert result == git.CommandResult(37, 1, output, "warning: α")
    arguments = launch.call_args.args[1]
    assert arguments[-5:] == [
        "status",
        "--porcelain=v2",
        "-z",
        "--",
        "-path[1].cpp",
    ]
    assert arguments[:2] == ["--no-pager", "--literal-pathspecs"]
    assert launch.call_args.kwargs["cwd"] == cpp_acceptance_project
    assert launch.call_args.kwargs["inherit_environment"] is False
    assert launch.call_args.kwargs["encoding"].driver == "latin_1"
    assert launch.call_args.kwargs["env"]["HOME"] == "operator-home"
    assert launch.call_args.kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    session.write_stdin.assert_awaited_once_with(
        "message α\n".encode("utf-8").decode("latin_1"),
    )
    session.close_stdin.assert_awaited_once()
    assert session.closed


def test_environment_removes_repository_redirection_and_keeps_operator_credentials(
    monkeypatch,
):
    """Remove repository/config injection while preserving operator credentials."""
    monkeypatch.setenv("GIT_DIR", "wrong-repository")
    monkeypatch.setenv("GIT_INDEX_FILE", "wrong-index")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "wrong-hooks")
    monkeypatch.setenv("GIT_TRACE", "private-trace-file")
    monkeypatch.setenv("GIT_GLOB_PATHSPECS", "1")
    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "0")
    selected = git.command_environment(
        {"GIT_WORK_TREE": "another-tree", "SSH_AUTH_SOCK": "operator-agent"},
        True,
        False,
    )
    assert selected["SSH_AUTH_SOCK"] == "operator-agent"
    assert selected["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_DIR" not in selected
    assert "GIT_WORK_TREE" not in selected
    assert "GIT_INDEX_FILE" not in selected
    assert "GIT_TRACE" not in selected
    assert "GIT_GLOB_PATHSPECS" not in selected
    assert "GIT_OPTIONAL_LOCKS" not in selected
    assert not any(key.startswith("GIT_CONFIG") for key in selected)


@pytest.mark.anyio
async def test_run_byte_limit_fails_without_returning_partial_result(cpp_acceptance_project):
    """Stop consumption and close the session rather than exposing a truncated Git record."""
    session = FragmentedSession(b"12345")
    launch = AsyncMock(return_value=session)
    methods = git.create_spec(
        cpp_acceptance_project / "git",
        SimpleNamespace(launch=launch),
    ).methods
    with pytest.raises(ToolCommandError, match="byte limit") as caught:
        await methods["run"](cpp_acceptance_project, ["status"], max_bytes=4)
    assert caught.value.process_id == 37
    assert "4 bytes" in str(caught.value) and "process_get(process_id=37)" in str(caught.value)
    assert session.closed


@pytest.mark.anyio
async def test_run_drains_stderr_with_a_bounded_summary(scripted_processes):
    """Drain long stderr fully while retaining only a small result summary."""
    scripted_processes.code = (
        "import sys; sys.stdout.buffer.write(b'path\\x00'); "
        "sys.stderr.buffer.write(b'e'*100000); sys.exit(3)"
    )
    methods = git.create_spec(Path(sys.executable), scripted_processes).methods
    result = await methods["run"](scripted_processes.root, ["status"])
    assert result.return_code == 3 and result.stdout == b"path\0"
    assert len(result.stderr) == 16 * 1024
    record = scripted_processes.records[result.process_id]
    assert sum(len(item.text) for item in record.transcript if item.stream == "stderr") == 100000
    assert record.status.current_status == 3


@pytest.mark.anyio
async def test_run_stdin_reaches_child_as_utf8(scripted_processes):
    """Encode command input as UTF-8 despite the process transport's byte carrier."""
    scripted_processes.code = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
    methods = git.create_spec(Path(sys.executable), scripted_processes).methods
    result = await methods["run"](scripted_processes.root, ["commit", "-F", "-"], stdin="α\nβ")
    assert result.stdout == "α\nβ".encode("utf-8")


@pytest.mark.anyio
async def test_run_timeout_retains_process_id_and_closes_child(scripted_processes):
    """Convert managed timeouts into command failures without duplicating transcripts."""
    scripted_processes.code = "import time; print('PRIVATE',flush=True); time.sleep(30)"
    methods = git.create_spec(Path(sys.executable), scripted_processes).methods
    with pytest.raises(ToolCommandError) as caught:
        await methods["run"](
            scripted_processes.root,
            ["fetch"],
            timeout=ProcessTimeout(total=0.1),
        )
    assert caught.value.process_id is not None
    assert "total=0.1 s" in str(caught.value)
    assert "PRIVATE" not in str(caught.value)
    assert all(
        record.status.current_status != "running"
        for record in scripted_processes.records.values()
    )


@pytest.mark.anyio
async def test_run_cancellation_closes_owned_child(scripted_processes):
    """Propagate cancellation after the context has stopped its process."""
    scripted_processes.code = "import time; time.sleep(30)"
    methods = git.create_spec(Path(sys.executable), scripted_processes).methods
    task = asyncio.create_task(methods["run"](scripted_processes.root, ["fetch"]))
    await scripted_processes.started.wait()
    task.cancel()
    outcomes = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(outcomes[0], asyncio.CancelledError)
    statuses = {record.status.current_status for record in scripted_processes.records.values()}
    assert statuses == {"stopped"}


@pytest.mark.anyio
async def test_version_method_remains_available(scripted_processes):
    """Preserve toolchain version discovery beside the new internal execution method."""
    scripted_processes.code = "print('git version 2.51.0.windows.1')"
    methods = git.create_spec(Path(sys.executable), scripted_processes).methods
    assert await methods["version"]() == "2.51.0.windows.1"


@pytest.mark.anyio
async def test_run_rejects_invalid_inputs_before_launch(cpp_acceptance_project):
    """Reject impossible framing and output limits without creating a process."""
    launch = AsyncMock()
    methods = git.create_spec(
        cpp_acceptance_project / "git",
        SimpleNamespace(launch=launch),
    ).methods
    for arguments, limit in (([], 5), (["show", "bad\0ref"], 5), (["status"], 0)):
        with pytest.raises(ToolCommandError):
            await methods["run"](cpp_acceptance_project, arguments, max_bytes=limit)
    launch.assert_not_awaited()


@pytest.mark.anyio
async def test_start_failure_explains_operator_recovery_without_native_paths(
    cpp_acceptance_project,
):
    """Name the unavailable Git executable role without copying native exception filenames."""
    launch = AsyncMock(side_effect=ProcessStartError("private executable path"))
    methods = git.create_spec(
        cpp_acceptance_project / "git",
        SimpleNamespace(launch=launch),
    ).methods
    with pytest.raises(ToolCommandError) as caught:
        await methods["run"](cpp_acceptance_project, ["status"])
    message = str(caught.value)
    assert "could not be started" in message and "operator" in message
    assert "toolset_get" in message and "private executable path" not in message
