"""Safe asyncio runtime for short external commands and streaming tool processes."""

from __future__ import annotations

import asyncio
import codecs
import ctypes
from ctypes import wintypes
import os
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import StructuredLogger
from forgemcp.models import ProcessOutput, ProcessResult
from forgemcp.processes.errors import (
    ProcessArgumentError,
    ProcessEnvironmentError,
    ProcessExecutableError,
    ProcessOwnershipError,
    ProcessPolicyError,
    ProcessRuntimeClosedError,
    ProcessWorkingDirectoryError,
)
from forgemcp.processes.policy import ProcessPolicy, _contains_link_or_reparse_point

if TYPE_CHECKING:
    from asyncio.streams import StreamReader, StreamWriter


def _known_llvm_quality_tools() -> tuple[Path, ...]:
    """Return fixed conventional LLVM quality-tool locations for policy approval.

    This intentionally does not scan arbitrary directories.  A candidate is
    still required to be an existing regular, non-link executable by
    ``ProcessPolicy`` before it becomes usable.
    """
    roots: list[Path] = []
    if os.name == "nt":
        roots.extend(
            Path(value)
            for value in (
                os.environ.get("ProgramFiles"),
                os.environ.get("ProgramW6432"),
                os.environ.get("ProgramFiles(x86)"),
            )
            if value
        )
        suffix = ".exe"
    else:
        return tuple(
            path
            for directory in (Path("/usr/bin"), Path("/usr/local/bin"), Path("/opt/llvm/bin"))
            for path in (directory / "clang-format", directory / "clang-tidy")
        )
    return tuple(root / "LLVM" / "bin" / f"{name}{suffix}" for root in roots for name in ("clang-format", "clang-tidy"))


class ProcessTreeOwnership(StrEnum):
    """Requested operating-system containment strength for one launch."""

    BEST_EFFORT = "best_effort"
    REQUIRED = "required"


class ProcessEnvironmentMode(StrEnum):
    """Environment construction mode for one launch."""

    INHERIT = "inherit"
    SCRUBBED = "scrubbed"


class _WindowsProcessJob:
    """Private Windows Job Object that kills a non-detached process tree on close."""

    _EXTENDED_LIMIT_INFORMATION = 9
    _KILL_ON_JOB_CLOSE = 0x00002000

    def __init__(self, handle: int) -> None:
        self._handle = handle

    @classmethod
    def create(cls) -> "_WindowsProcessJob | None":
        """Create a non-breakaway kill-on-close job before launching its member."""
        if os.name != "nt":
            return None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

            class _BasicLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("per_process_user_time_limit", ctypes.c_longlong),
                    ("per_job_user_time_limit", ctypes.c_longlong),
                    ("limit_flags", wintypes.DWORD),
                    ("minimum_working_set_size", ctypes.c_size_t),
                    ("maximum_working_set_size", ctypes.c_size_t),
                    ("active_process_limit", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t),
                    ("priority_class", wintypes.DWORD),
                    ("scheduling_class", wintypes.DWORD),
                ]

            class _IoCounters(ctypes.Structure):
                _fields_ = [
                    ("read_operation_count", ctypes.c_ulonglong),
                    ("write_operation_count", ctypes.c_ulonglong),
                    ("other_operation_count", ctypes.c_ulonglong),
                    ("read_transfer_count", ctypes.c_ulonglong),
                    ("write_transfer_count", ctypes.c_ulonglong),
                    ("other_transfer_count", ctypes.c_ulonglong),
                ]

            class _ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("basic_limit_information", _BasicLimitInformation),
                    ("io_info", _IoCounters),
                    ("process_memory_limit", ctypes.c_size_t),
                    ("job_memory_limit", ctypes.c_size_t),
                    ("peak_process_memory_used", ctypes.c_size_t),
                    ("peak_job_memory_used", ctypes.c_size_t),
                ]

            kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            )
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL

            job_handle = kernel32.CreateJobObjectW(None, None)
            if not job_handle:
                return None
            limits = _ExtendedLimitInformation()
            limits.basic_limit_information.limit_flags = cls._KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                job_handle,
                cls._EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                kernel32.CloseHandle(job_handle)
                return None
            return cls(int(job_handle))
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def attach(self, process: asyncio.subprocess.Process) -> bool:
        """Assign a just-created child and report assignment success exactly."""
        if not self._handle:
            return False
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            popen = process._transport.get_extra_info("subprocess")  # type: ignore[attr-defined]
            process_handle = getattr(popen, "_handle", None)
            return bool(
                process_handle is not None
                and kernel32.AssignProcessToJobObject(
                    wintypes.HANDLE(self._handle), wintypes.HANDLE(int(process_handle))
                )
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def close(self) -> None:
        """Close the job once; Windows terminates remaining member processes."""
        if not self._handle:
            return
        handle = self._handle
        self._handle = 0
        try:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(handle))
        except OSError:
            return


class _BoundedTextCapture:
    """Incrementally decode one stream while retaining at most a character limit."""

    def __init__(self, maximum_characters: int) -> None:
        self._maximum_characters = maximum_characters
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._parts: list[str] = []
        self._length = 0
        self._truncated = False

    def feed(self, data: bytes) -> None:
        """Consume bytes without retaining data beyond the configured character cap."""
        self._append(self._decoder.decode(data, final=False))

    def finish(self) -> None:
        """Flush a partial UTF-8 sequence as a replacement character if necessary."""
        self._append(self._decoder.decode(b"", final=True))

    def discard_remaining(self) -> None:
        """Mark a capture incomplete after forced stream-reader shutdown."""
        self._truncated = True

    def to_model(self) -> ProcessOutput:
        """Construct the transport-neutral bounded representation."""
        return ProcessOutput(text="".join(self._parts), truncated=self._truncated)

    def _append(self, text: str) -> None:
        if not text:
            return
        remaining = self._maximum_characters - self._length
        if remaining <= 0:
            self._truncated = True
            return
        accepted = text[:remaining]
        self._parts.append(accepted)
        self._length += len(accepted)
        if len(accepted) < len(text):
            self._truncated = True


class ProcessHandle:
    """Streaming handle for a long-lived clangd or DAP-adapter process.

    ``stdin``, ``stdout``, and ``stderr`` are asyncio streams so protocol
    adapters can retain framing control.  Unlike :meth:`ProcessRuntime.run`,
    this handle never accumulates output or produces a ``ProcessResult``.
    Call :meth:`aclose` when the protocol session ends, or let
    :meth:`ProcessRuntime.aclose` close all still-live handles at shutdown.
    """

    def __init__(
        self,
        runtime: "ProcessRuntime",
        process: asyncio.subprocess.Process,
        job: _WindowsProcessJob | None = None,
        *,
        required_ownership: bool = False,
        ownership_established: bool = False,
        environment_mode: ProcessEnvironmentMode = ProcessEnvironmentMode.INHERIT,
    ) -> None:
        self._runtime = runtime
        self._process = process
        self._job = job
        self._closed = False
        self._required_ownership = required_ownership
        self._ownership_established = ownership_established
        self._environment_mode = environment_mode

    @property
    def pid(self) -> int:
        """Return the operating-system process identifier."""
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        """Return the exit status once the process has ended."""
        return self._process.returncode

    @property
    def required_ownership(self) -> bool:
        """Return whether this launch required containment before the handle was returned."""
        return self._required_ownership

    @property
    def ownership_established(self) -> bool:
        """Return whether the platform containment primitive was configured for this handle."""
        return self._ownership_established

    @property
    def environment_mode(self) -> ProcessEnvironmentMode:
        """Return whether this child inherited or received a scrubbed environment."""
        return self._environment_mode

    @property
    def stdin(self) -> "StreamWriter":
        """Return the writable protocol input stream."""
        assert self._process.stdin is not None
        return self._process.stdin

    @property
    def stdout(self) -> "StreamReader":
        """Return the readable protocol output stream."""
        assert self._process.stdout is not None
        return self._process.stdout

    @property
    def stderr(self) -> "StreamReader":
        """Return the readable diagnostic stream."""
        assert self._process.stderr is not None
        return self._process.stderr

    async def wait(self, *, timeout_seconds: float | None = None) -> int:
        """Wait for normal process exit without silently terminating a protocol session."""
        timeout = self._runtime._validate_handle_timeout(timeout_seconds)
        if timeout is None:
            exit_code = await self._process.wait()
        else:
            exit_code = await asyncio.wait_for(self._process.wait(), timeout=timeout)
        # A session leader can exit before one of its descendants.  Windows
        # closes the Job Object below; POSIX must explicitly remove the still
        # live session/process group before this handle is forgotten.
        if os.name != "nt" and self._runtime._posix_group_exists(self._process.pid):
            await self._runtime._force_kill_process_tree(self._process, self._job)
        self._runtime._forget(self)
        return exit_code

    async def terminate(self) -> None:
        """Gracefully stop the process tree, escalating to a forced tree kill if needed."""
        await self._runtime._terminate_process_tree(self._process, self._job)
        self._runtime._forget(self)

    async def kill(self) -> None:
        """Immediately force-kill the process tree and wait for the direct child to reap."""
        await self._runtime._force_kill_process_tree(self._process, self._job)
        await self._wait_for_direct_process()
        self._runtime._forget(self)

    async def aclose(self) -> None:
        """Close stdin and end a running process tree; safe to call repeatedly."""
        if self._closed:
            return
        self._closed = True
        self._close_stdin()
        if self._process.returncode is None or (
            os.name != "nt" and self._runtime._posix_group_exists(self._process.pid)
        ):
            await self._runtime._terminate_process_tree(self._process, self._job)
        self._runtime._forget(self)

    def close_nowait(self) -> None:
        """Start immediate best-effort shutdown for synchronous application teardown."""
        if self._closed:
            return
        self._closed = True
        self._close_stdin()
        if os.name == "nt" or self._runtime._posix_group_exists(self._process.pid):
            self._runtime._signal_process_group_nowait(self._process, force=False)
        self._runtime._forget(self)

    async def _wait_for_direct_process(self) -> None:
        try:
            await self._process.wait()
        except ProcessLookupError:
            return

    def _close_stdin(self) -> None:
        if self._process.stdin is not None and not self._process.stdin.is_closing():
            self._process.stdin.close()


class ProcessRuntime:
    """Run allow-listed tools with workspace-scoped CWD and safe lifecycle control.

    ``run`` is for short commands and returns bounded, separate stdout/stderr
    captures.  ``start`` is for protocol processes and returns a streaming
    :class:`ProcessHandle`.  All launches use ``asyncio.create_subprocess_exec``
    with an argv sequence and ``shell=False``.
    """

    def __init__(
        self,
        config: ForgeConfig,
        logger: StructuredLogger,
        *,
        policy: ProcessPolicy | None = None,
    ) -> None:
        """Bind the runtime to one validated workspace and explicit policy."""
        self._root = config.workspace_root
        self._logger = logger
        configured_exact_paths = frozenset(
            path
            for path in (
                config.clangd_path,
                config.lldb_dap_path,
                config.clang_format_path,
                config.clang_tidy_path,
                *_known_llvm_quality_tools(),
            )
            if path is not None and path.is_file() and not path.is_symlink()
        )
        self._policy = (
            ProcessPolicy(
                allowed_executable_paths=configured_exact_paths
            )
            if policy is None
            else policy
        )
        self._base_environment = dict(os.environ)
        self._executable_search_path = self._base_environment.get("PATH")
        self._handles: set[ProcessHandle] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def workspace_root(self) -> Path:
        """Return the resolved root to which all working directories are scoped."""
        return self._root

    @property
    def policy(self) -> ProcessPolicy:
        """Return the immutable policy active for this runtime."""
        return self._policy

    def resolve_executable(self, executable: str) -> str:
        """Return the policy-qualified canonical executable path without launching it.

        Feature modules use this only for status metadata after selecting a
        fixed tool name or operator-configured path; it never accepts MCP
        input and retains the same basename/exact approval checks as ``run``.
        """
        return self._prepare_argv((executable,))[0]

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        environment: Mapping[str, str] | None = None,
        inherit_environment: bool | None = None,
        timeout_seconds: float | None = None,
        ownership: ProcessTreeOwnership = ProcessTreeOwnership.BEST_EFFORT,
        environment_mode: ProcessEnvironmentMode = ProcessEnvironmentMode.INHERIT,
        approved_path_directories: Sequence[Path] = (),
        require_exact_executable: bool = False,
    ) -> ProcessResult:
        """Run one bounded short command and return its completed result.

        A timeout or caller cancellation terminates the process tree.  Timeout
        is represented in the returned model; cancellation is propagated after
        cleanup so the caller retains normal asyncio cancellation semantics.
        """
        timeout = self._validate_run_timeout(timeout_seconds)
        handle = await self.start(
            argv,
            cwd=cwd,
            environment=environment,
            inherit_environment=inherit_environment,
            ownership=ownership,
            environment_mode=environment_mode,
            approved_path_directories=approved_path_directories,
            require_exact_executable=require_exact_executable,
        )
        started_at = datetime.now(UTC)
        stdout_capture = _BoundedTextCapture(self._policy.max_output_characters)
        stderr_capture = _BoundedTextCapture(self._policy.max_output_characters)
        stdout_task = asyncio.create_task(self._capture_stream(handle.stdout, stdout_capture))
        stderr_task = asyncio.create_task(self._capture_stream(handle.stderr, stderr_capture))
        capture_tasks = (stdout_task, stderr_task)
        timed_out = False
        exit_code: int | None = None

        try:
            try:
                exit_code = await asyncio.wait_for(handle._process.wait(), timeout=timeout)
            except TimeoutError:
                timed_out = True
                await self._terminate_process_tree(handle._process, handle._job)
            await self._finish_captures(
                capture_tasks, (stdout_capture, stderr_capture), handle._process, handle._job
            )
            if os.name != "nt" and self._posix_group_exists(handle.pid):
                await self._force_kill_process_tree(handle._process, handle._job)
        except asyncio.CancelledError:
            await asyncio.shield(self._terminate_process_tree(handle._process, handle._job))
            await asyncio.shield(
                self._finish_captures(
                    capture_tasks, (stdout_capture, stderr_capture), handle._process, handle._job
                )
            )
            raise
        finally:
            self._forget(handle)

        finished_at = datetime.now(UTC)
        result = ProcessResult(
            exit_code=None if timed_out else exit_code,
            timed_out=timed_out,
            started_at=started_at,
            finished_at=finished_at,
            stdout=stdout_capture.to_model(),
            stderr=stderr_capture.to_model(),
        )
        self._logger.info(
            "process_finished",
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            stdout_characters=len(result.stdout.text),
            stdout_truncated=result.stdout.truncated,
            stderr_characters=len(result.stderr.text),
            stderr_truncated=result.stderr.truncated,
        )
        return result

    async def start(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        environment: Mapping[str, str] | None = None,
        inherit_environment: bool | None = None,
        ownership: ProcessTreeOwnership = ProcessTreeOwnership.BEST_EFFORT,
        environment_mode: ProcessEnvironmentMode = ProcessEnvironmentMode.INHERIT,
        approved_path_directories: Sequence[Path] = (),
        require_exact_executable: bool = False,
    ) -> ProcessHandle:
        """Start an allow-listed protocol process with streaming stdin/stdout/stderr.

        Normal callers retain the inherited environment and best-effort tree
        cleanup.  Trusted protocol adapters opt into ``REQUIRED`` ownership,
        a scrubbed environment, and an exact approved executable through
        :meth:`start_trusted_adapter`.
        """
        if self._closed:
            raise ProcessRuntimeClosedError("The process runtime is closing and cannot start processes.")
        requested_ownership = self._normalise_ownership(ownership)
        requested_environment_mode = self._normalise_environment_mode(environment_mode)
        if not isinstance(require_exact_executable, bool):
            raise ProcessPolicyError("Exact executable enforcement must be a boolean.")
        executable_argv = self._prepare_argv(argv, require_exact_executable=require_exact_executable)
        working_directory = self._resolve_working_directory(cwd)
        child_environment = self._prepare_environment(
            environment,
            inherit_environment,
            mode=requested_environment_mode,
            executable=Path(executable_argv[0]),
            approved_path_directories=approved_path_directories,
        )
        job = _WindowsProcessJob.create()
        if os.name == "nt" and requested_ownership is ProcessTreeOwnership.REQUIRED and job is None:
            raise ProcessOwnershipError("Required process-tree ownership could not be established.")
        try:
            process = await self._create_process(executable_argv, working_directory, child_environment)
        except BaseException:
            if job is not None:
                job.close()
            raise

        ownership_established = os.name != "nt"
        if os.name == "nt":
            if job is None or not job.attach(process):
                if job is not None:
                    job.close()
                job = None
                if requested_ownership is ProcessTreeOwnership.REQUIRED:
                    await self._terminate_uncontained_process(process)
                    raise ProcessOwnershipError("Required process-tree ownership could not be established.")
            else:
                ownership_established = True

        handle = ProcessHandle(
            self,
            process,
            job,
            required_ownership=requested_ownership is ProcessTreeOwnership.REQUIRED,
            ownership_established=ownership_established,
            environment_mode=requested_environment_mode,
        )
        if self._closed:
            # Shutdown may begin while create_subprocess_exec is awaiting the
            # operating system.  Do not let that race orphan the new child.
            await handle.aclose()
            raise ProcessRuntimeClosedError("The process runtime is closing and cannot start processes.")
        self._handles.add(handle)
        self._logger.info("process_started", pid=process.pid)
        return handle

    async def start_trusted_adapter(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        environment: Mapping[str, str] | None = None,
        approved_path_directories: Sequence[Path] = (),
    ) -> ProcessHandle:
        """Start a trusted protocol adapter only after strict containment is ready."""
        return await self.start(
            argv,
            cwd=cwd,
            environment=environment,
            ownership=ProcessTreeOwnership.REQUIRED,
            environment_mode=ProcessEnvironmentMode.SCRUBBED,
            approved_path_directories=approved_path_directories,
            require_exact_executable=True,
        )

    async def run_trusted_adapter(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        environment: Mapping[str, str] | None = None,
        approved_path_directories: Sequence[Path] = (),
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        """Run a bounded adapter probe with exact approval and strict containment."""
        return await self.run(
            argv,
            cwd=cwd,
            environment=environment,
            timeout_seconds=timeout_seconds,
            ownership=ProcessTreeOwnership.REQUIRED,
            environment_mode=ProcessEnvironmentMode.SCRUBBED,
            approved_path_directories=approved_path_directories,
            require_exact_executable=True,
        )

    async def aclose(self) -> None:
        """Await clean shutdown of every live process handle owned by this runtime."""
        self._closed = True
        handles = tuple(self._handles)
        if handles:
            await asyncio.gather(*(handle.aclose() for handle in handles), return_exceptions=True)

    def close(self) -> None:
        """Begin best-effort shutdown when the host lifecycle is synchronous.

        Hosts with an async shutdown hook must use :meth:`aclose` so Windows
        tree termination can await ``taskkill`` and all direct children can be
        reaped.  This method still immediately signals every tracked group.
        """
        if self._closed:
            return
        self._closed = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            for handle in tuple(self._handles):
                handle.close_nowait()
            return
        self._close_task = loop.create_task(self.aclose())

    async def _create_process(
        self, argv: tuple[str, ...], cwd: Path, environment: Mapping[str, str]
    ) -> asyncio.subprocess.Process:
        kwargs: dict[str, object] = {"shell": False}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            return await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=dict(environment),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except (FileNotFoundError, PermissionError, OSError) as error:
            raise ProcessExecutableError("The approved executable could not be started.") from error

    def _prepare_argv(
        self, argv: Sequence[str], *, require_exact_executable: bool = False
    ) -> tuple[str, ...]:
        if isinstance(argv, str) or not isinstance(argv, Sequence) or not argv:
            raise ProcessArgumentError("Commands must be a non-empty argv sequence, never a shell string.")
        values = tuple(argv)
        if any(not isinstance(value, str) or "\x00" in value for value in values):
            raise ProcessArgumentError("Every argv value must be a NUL-free string.")
        if not values[0]:
            raise ProcessArgumentError("The executable argv value must not be empty.")

        requested = values[0]
        requested_path = Path(requested)
        if require_exact_executable and not requested_path.is_absolute():
            raise ProcessExecutableError("Trusted adapters must use an exact approved absolute executable path.")
        resolved = self._resolve_executable(requested)
        if not self._is_allowed_executable(requested, requested_path, resolved):
            raise ProcessExecutableError("The requested executable is not allowed by process policy.")
        return (str(resolved), *values[1:])

    def _resolve_executable(self, requested: str) -> Path:
        candidate = Path(requested)
        windows = PureWindowsPath(requested)
        posix = PurePosixPath(requested)
        if bool(windows.drive) or bool(windows.root) or windows.is_absolute() or posix.is_absolute():
            if not candidate.is_absolute():
                raise ProcessExecutableError("Executable paths must be absolute paths.")
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if not resolved.is_file() or (os.name != "nt" and not os.access(resolved, os.X_OK)):
                raise ProcessExecutableError("The requested executable is not available.")
            return resolved

        if "/" in requested or "\\" in requested:
            raise ProcessExecutableError("Executable paths must be explicitly allow-listed absolute paths.")
        discovered = shutil.which(requested, path=self._executable_search_path)
        if discovered is None:
            raise ProcessExecutableError("The requested executable is not available.")
        return Path(discovered).resolve()

    def _is_allowed_executable(self, requested: str, requested_path: Path, resolved: Path) -> bool:
        if requested_path.is_absolute() and self._policy.approves_exact_executable(requested_path):
            return True
        if "/" in requested or "\\" in requested or Path(requested).is_absolute():
            return False
        return self._policy.allows_executable_name(requested)

    def _resolve_working_directory(self, cwd: str) -> Path:
        if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
            raise ProcessWorkingDirectoryError("Working directories must be NUL-free workspace-relative paths.")
        native = Path(cwd)
        windows = PureWindowsPath(cwd)
        posix = PurePosixPath(cwd)
        if (
            native.is_absolute()
            or bool(native.anchor)
            or bool(windows.drive)
            or bool(windows.root)
            or windows.is_absolute()
            or posix.is_absolute()
        ):
            raise ProcessWorkingDirectoryError("Working directories must be relative to the workspace.")
        parts = tuple(part for part in native.parts if part not in {"", "."})
        if any(part == ".." for part in parts):
            raise ProcessWorkingDirectoryError("Working directories must not contain parent traversal.")

        candidate = self._root
        for part in parts:
            candidate = candidate / part
            if candidate.is_symlink():
                raise ProcessWorkingDirectoryError("Working directories must not traverse symlinks.")
        try:
            resolved = candidate.resolve(strict=True)
            relative = resolved.relative_to(self._root).as_posix() or "."
        except (FileNotFoundError, ValueError) as error:
            raise ProcessWorkingDirectoryError(
                "The requested working directory must be an existing workspace directory."
            ) from error
        if not resolved.is_dir():
            raise ProcessWorkingDirectoryError("The requested working directory is not a directory.")
        if not self._policy.allows_working_directory(relative):
            raise ProcessWorkingDirectoryError("The requested working directory is not allowed by policy.")
        return resolved

    def _prepare_environment(
        self,
        environment: Mapping[str, str] | None,
        inherit_environment: bool | None,
        *,
        mode: ProcessEnvironmentMode,
        executable: Path,
        approved_path_directories: Sequence[Path],
    ) -> dict[str, str]:
        if mode is ProcessEnvironmentMode.SCRUBBED:
            if inherit_environment is not None:
                raise ProcessEnvironmentError(
                    "Scrubbed adapter environments cannot inherit the host environment."
                )
            return self._prepare_scrubbed_environment(
                environment,
                executable=executable,
                approved_path_directories=approved_path_directories,
            )
        inherit = True if inherit_environment is None else inherit_environment
        if not isinstance(inherit, bool):
            raise ProcessEnvironmentError("Environment inheritance must be a boolean.")
        if inherit and not self._policy.allow_environment_inheritance:
            raise ProcessEnvironmentError("Environment inheritance is disabled by process policy.")
        if environment is not None and not isinstance(environment, Mapping):
            raise ProcessEnvironmentError("Environment overrides must be a string mapping.")
        overrides = {} if environment is None else dict(environment)
        for key, value in overrides.items():
            if (
                not isinstance(key, str)
                or not key
                or "\x00" in key
                or "=" in key
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise ProcessEnvironmentError(
                    "Environment names and values must be NUL-free strings."
                )
            allowed = self._policy.allowed_environment_overrides
            if allowed is not None and key not in allowed:
                raise ProcessEnvironmentError("An environment override is not allowed by process policy.")
        values = dict(self._base_environment) if inherit else {}
        values.update(overrides)
        return values

    def _prepare_scrubbed_environment(
        self,
        environment: Mapping[str, str] | None,
        *,
        executable: Path,
        approved_path_directories: Sequence[Path],
    ) -> dict[str, str]:
        """Construct the small adapter environment without inheriting host secrets."""
        overrides = self._validate_environment_overrides(environment)
        if any(key.casefold() == "path" for key in overrides):
            raise ProcessEnvironmentError("Adapter PATH is constructed only from approved directories.")

        if os.name == "nt":
            system_keys = ("SystemRoot", "WINDIR", "ComSpec", "TEMP", "TMP", "PATHEXT")
        else:
            system_keys = ("TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
        values = {
            key: value
            for key in system_keys
            if (value := self._base_environment_value(key)) is not None
        }
        directories = (executable.parent, *approved_path_directories)
        approved = self._normalise_adapter_path_directories(directories)
        values["PATH"] = os.pathsep.join(str(directory) for directory in approved)
        values.update(overrides)
        return values

    def _validate_environment_overrides(
        self, environment: Mapping[str, str] | None
    ) -> dict[str, str]:
        if environment is not None and not isinstance(environment, Mapping):
            raise ProcessEnvironmentError("Environment overrides must be a string mapping.")
        overrides = {} if environment is None else dict(environment)
        for key, value in overrides.items():
            if (
                not isinstance(key, str)
                or not key
                or "\x00" in key
                or "=" in key
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise ProcessEnvironmentError(
                    "Environment names and values must be NUL-free strings."
                )
            allowed = self._policy.allowed_environment_overrides
            if allowed is not None and key not in allowed:
                raise ProcessEnvironmentError("An environment override is not allowed by process policy.")
        return overrides

    def _base_environment_value(self, key: str) -> str | None:
        if os.name != "nt":
            return self._base_environment.get(key)
        wanted = key.casefold()
        return next(
            (value for name, value in self._base_environment.items() if name.casefold() == wanted),
            None,
        )

    def _normalise_adapter_path_directories(self, directories: Sequence[Path]) -> tuple[Path, ...]:
        normalised: list[Path] = []
        keys: set[str] = set()
        for directory in directories:
            if not isinstance(directory, Path) or not directory.is_absolute():
                raise ProcessEnvironmentError("Adapter PATH directories must be approved absolute directories.")
            try:
                resolved = directory.resolve(strict=True)
            except (FileNotFoundError, OSError) as error:
                raise ProcessEnvironmentError("Adapter PATH directories must exist.") from error
            if _contains_link_or_reparse_point(directory) or not resolved.is_dir():
                raise ProcessEnvironmentError("Adapter PATH directories must be non-symlink directories.")
            key = str(resolved).casefold() if os.name == "nt" else str(resolved)
            if key not in keys:
                keys.add(key)
                normalised.append(resolved)
        return tuple(normalised)

    @staticmethod
    def _normalise_ownership(value: ProcessTreeOwnership) -> ProcessTreeOwnership:
        try:
            return ProcessTreeOwnership(value)
        except (TypeError, ValueError) as error:
            raise ProcessPolicyError("Process ownership must be best_effort or required.") from error

    @staticmethod
    def _normalise_environment_mode(value: ProcessEnvironmentMode) -> ProcessEnvironmentMode:
        try:
            return ProcessEnvironmentMode(value)
        except (TypeError, ValueError) as error:
            raise ProcessPolicyError("Process environment mode must be inherit or scrubbed.") from error

    def _validate_run_timeout(self, timeout_seconds: float | None) -> float:
        timeout = self._policy.default_timeout_seconds if timeout_seconds is None else timeout_seconds
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ProcessPolicyError("Process timeout must be greater than zero.")
        if timeout > self._policy.maximum_timeout_seconds:
            raise ProcessPolicyError("Process timeout exceeds the configured maximum.")
        return float(timeout)

    def _validate_handle_timeout(self, timeout_seconds: float | None) -> float | None:
        if timeout_seconds is None:
            return None
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ProcessPolicyError("Process timeout must be greater than zero.")
        if timeout_seconds > self._policy.maximum_timeout_seconds:
            raise ProcessPolicyError("Process timeout exceeds the configured maximum.")
        return float(timeout_seconds)

    async def _capture_stream(self, stream: "StreamReader", capture: _BoundedTextCapture) -> None:
        try:
            while data := await stream.read(65_536):
                capture.feed(data)
        finally:
            capture.finish()

    async def _finish_captures(
        self,
        tasks: tuple[asyncio.Task[None], asyncio.Task[None]],
        captures: tuple[_BoundedTextCapture, _BoundedTextCapture],
        process: asyncio.subprocess.Process,
        job: _WindowsProcessJob | None,
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(*tasks)),
                timeout=self._policy.stream_close_timeout_seconds,
            )
            return
        except TimeoutError:
            # A descendant still holding the pipes makes a short command unsafe
            # to return from.  Escalate its isolated process tree before giving up.
            await self._force_kill_process_tree(process, job)
        try:
            await asyncio.wait_for(
                asyncio.shield(asyncio.gather(*tasks)),
                timeout=self._policy.termination_grace_seconds,
            )
        except TimeoutError:
            for capture in captures:
                capture.discard_remaining()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _terminate_process_tree(
        self,
        process: asyncio.subprocess.Process,
        job: _WindowsProcessJob | None,
    ) -> None:
        if os.name != "nt" and not self._posix_group_exists(process.pid):
            return
        self._signal_process_group_nowait(process, force=False)
        if os.name != "nt":
            # The direct adapter may exit after TERM while a descendant keeps
            # the group (and protocol pipes) alive.  Required POSIX cleanup is
            # therefore group-based, never inferred from the parent exit.
            if not await self._wait_for_posix_group(process.pid, self._policy.termination_grace_seconds):
                self._signal_process_group_nowait(process, force=True)
            await self._wait_for_direct_process(process)
            return
        if await self._wait_for_process(process, self._policy.termination_grace_seconds):
            if job is not None:
                job.close()
            return
        await self._force_kill_process_tree(process, job)
        await self._wait_for_direct_process(process)

    @staticmethod
    async def _terminate_uncontained_process(process: asyncio.subprocess.Process) -> None:
        """Immediately reap a child when required Windows Job assignment failed.

        This deliberately does not claim descendant cleanup: without a Job
        Object, ``taskkill`` is not a substitute for required ownership.  The
        process is never exposed through a handle in this failure path.
        """
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await process.wait()
        except ProcessLookupError:
            return

    async def _force_kill_process_tree(
        self,
        process: asyncio.subprocess.Process,
        job: _WindowsProcessJob | None,
    ) -> None:
        if os.name == "nt":
            if job is not None:
                job.close()
            else:
                await self._taskkill_tree(process.pid)
        elif self._posix_group_exists(process.pid):
            self._signal_process_group_nowait(process, force=True)
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass

    def _signal_process_group_nowait(
        self, process: asyncio.subprocess.Process, *, force: bool
    ) -> None:
        if os.name == "nt":
            if force:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            except (AttributeError, OSError, ProcessLookupError):
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            return
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            return

    async def _taskkill_tree(self, pid: int) -> None:
        """Use Windows' built-in tree terminator without introducing psutil."""
        try:
            taskkill = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (FileNotFoundError, OSError):
            return
        try:
            await asyncio.wait_for(taskkill.wait(), timeout=self._policy.termination_grace_seconds)
        except TimeoutError:
            try:
                taskkill.kill()
            except ProcessLookupError:
                pass
            await taskkill.wait()

    @staticmethod
    async def _wait_for_process(process: asyncio.subprocess.Process, timeout: float) -> bool:
        if process.returncode is not None:
            return True
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return True

    @staticmethod
    async def _wait_for_direct_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            await process.wait()
        except ProcessLookupError:
            return

    @staticmethod
    def _posix_group_exists(pid: int) -> bool:
        """Return whether the session-created POSIX group still exists."""
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        return True

    @staticmethod
    async def _wait_for_posix_group(pid: int, timeout: float) -> bool:
        """Wait briefly for every process in one session-created group to exit."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            if not ProcessRuntime._posix_group_exists(pid):
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(min(0.05, max(0.0, deadline - asyncio.get_running_loop().time())))

    def _forget(self, handle: ProcessHandle) -> None:
        self._handles.discard(handle)
        if handle._job is not None:
            handle._job.close()
