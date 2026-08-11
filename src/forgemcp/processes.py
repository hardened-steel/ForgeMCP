"""Controlled execution of external developer tools."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from forgemcp.config import ForgeConfig
from forgemcp.workspace import WorkspaceService


@dataclass(frozen=True, slots=True)
class ProcessResult:
    command: tuple[str, ...]
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    output_truncated: bool


@dataclass(frozen=True, slots=True)
class ProcessProgress:
    """A transport-neutral progress update emitted during a process run."""

    completed: float
    total: float | None
    message: str


ProgressReporter = Callable[[ProcessProgress], Awaitable[None]]
ProgressParser = Callable[[str, str], ProcessProgress | None]


class ProcessManager:
    """Runs explicit argument vectors; shell execution is deliberately unsupported."""

    def __init__(self, config: ForgeConfig, workspace: WorkspaceService) -> None:
        self._config = config
        self._workspace = workspace

    async def run(
        self,
        command: Sequence[str],
        *,
        cwd: str = ".",
        timeout_seconds: float | None = None,
        environment: Mapping[str, str] | None = None,
        progress_reporter: ProgressReporter | None = None,
        progress_parser: ProgressParser | None = None,
    ) -> ProcessResult:
        """Run one program in the workspace and return bounded captured output."""
        if not command or not all(command):
            raise ValueError("command must contain a program and non-empty arguments.")

        timeout = timeout_seconds or self._config.process_timeout_seconds
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive.")

        working_directory = self._workspace.resolve_path(cwd)
        if not working_directory.is_dir():
            raise ValueError(f"Not a directory: {cwd}")

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=working_directory,
            env={**os.environ, **environment} if environment else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._report_progress(
            progress_reporter,
            ProcessProgress(0, 1, f"Started: {command[0]}"),
        )

        output_limit = max(1, self._config.max_process_output_chars // 2)
        stdout_task = asyncio.create_task(
            self._read_stream(process.stdout, output_limit, "stdout", progress_reporter, progress_parser)
        )
        stderr_task = asyncio.create_task(
            self._read_stream(process.stderr, output_limit, "stderr", progress_reporter, progress_parser)
        )
        timed_out = False
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except TimeoutError:
            timed_out = True
            process.kill()
            await process.wait()

        stdout, stdout_truncated = await stdout_task
        stderr, stderr_truncated = await stderr_task
        output_truncated = stdout_truncated or stderr_truncated
        completion_message = "Timed out" if timed_out else f"Finished with exit code {process.returncode}"
        await self._report_progress(progress_reporter, ProcessProgress(1, 1, completion_message))

        return ProcessResult(
            command=tuple(command),
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            output_truncated=output_truncated,
        )

    async def _read_stream(
        self,
        stream: asyncio.StreamReader | None,
        limit: int,
        stream_name: str,
        reporter: ProgressReporter | None,
        parser: ProgressParser | None,
    ) -> tuple[str, bool]:
        if stream is None:
            return "", False

        chunks: list[str] = []
        stored_size = 0
        truncated = False
        while chunk := await stream.readline():
            text = chunk.decode("utf-8", errors="replace")
            if parser is not None:
                update = parser(text, stream_name)
                if update is not None:
                    await self._report_progress(reporter, update)
            remaining = limit - stored_size
            if remaining > 0:
                chunks.append(text[:remaining])
                stored_size += min(len(text), remaining)
            if len(text) > remaining:
                truncated = True
        return "".join(chunks), truncated

    @staticmethod
    async def _report_progress(
        reporter: ProgressReporter | None, update: ProcessProgress
    ) -> None:
        if reporter is not None:
            await reporter(update)
