"""Asynchronous subprocess lifecycle, transcript, and MCP inspection."""

from __future__ import annotations

import asyncio
import codecs
import locale
import logging
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.types import (
    Completion,
    CompletionArgument,
    CompletionContext,
    PromptReference,
    ResourceTemplateReference,
    ToolAnnotations,
)

from forgemcp.assets import IconFile, Widget
from forgemcp.completion import Complete

from .errors import (
    ProcessExitedError,
    ProcessNotFoundError,
    ProcessStartError,
    ProcessStreamError,
)
from .models import (
    ProcessDetails,
    ProcessLogEntry,
    ProcessOutput,
    ProcessOverview,
    ProcessReadMode,
    ProcessResult,
    ProcessStatus,
    ProcessStream,
    ProcessSummary,
    ProcessTimeoutMode,
    ProcessTranscriptItem,
)

logger = logging.getLogger(__name__)


@dataclass
class ProcessRecord:
    """Mutable internal state owned by ProcessService."""

    process_id: str
    executable: str
    arguments: tuple[str, ...]
    cwd: Path
    encoding: str
    encoding_errors: Literal["strict", "replace"]
    read_mode: ProcessReadMode
    raw_stdout: bool
    timeout: float | None
    timeout_mode: ProcessTimeoutMode
    status: ProcessStatus = ProcessStatus.STARTING
    process: asyncio.subprocess.Process | None = None
    pid: int | None = None
    return_code: int | None = None
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    started_monotonic: float = 0.0
    finished_monotonic: float | None = None
    last_output_monotonic: float = 0.0
    transcript: list[ProcessLogEntry] = field(default_factory=list)
    next_sequence: int = 1
    had_decoding_errors: bool = False
    output_claimed: bool = False
    stdout_reading: bool = False
    stderr_reading: bool = False
    stdout_eof: bool = False
    stderr_eof: bool = False
    output_queue: asyncio.Queue[ProcessOutput | object] | None = None
    stdout_queue: asyncio.Queue[str | bytes | object] | None = None
    stderr_queue: asyncio.Queue[str | object] | None = None
    reader_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    wait_task: asyncio.Task[None] | None = None
    timeout_task: asyncio.Task[None] | None = None
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stdin_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stdin_decoder: codecs.IncrementalDecoder | None = None


class ProcessService:
    """Run external development tools and expose their state without allowing arbitrary execution."""

    WIDGET = Widget("assets/process-overview.html")
    ICON = IconFile("icons/process.svg")
    RESOURCE_URI = "forgemcp://processes/{process_id}"
    QUEUE_SIZE = 128
    READ_SIZE = 64 * 1024
    TERMINATE_GRACE_SECONDS = 2.0
    EOF = object()
    RUNNING_STATUSES = frozenset({ProcessStatus.STARTING, ProcessStatus.RUNNING})

    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root.resolve()
        self.records: dict[str, ProcessRecord] = {}

    async def start(
        self,
        executable: str | Path,
        arguments: Sequence[str] = (),
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        encoding: str | None = None,
        encoding_errors: Literal["strict", "replace"] = "replace",
        timeout: float | None = None,
        timeout_mode: ProcessTimeoutMode = "total",
    ) -> ProcessSession:
        """Start a process whose stdout and stderr are consumed through output()."""
        record = await self.launch(
            executable,
            arguments,
            cwd=cwd,
            env=env,
            encoding=encoding,
            encoding_errors=encoding_errors,
            timeout=timeout,
            timeout_mode=timeout_mode,
            read_mode="combined",
            raw_stdout=False,
        )
        return ProcessSession(self, record.process_id)

    async def start_protocol(
        self,
        executable: str | Path,
        arguments: Sequence[str] = (),
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        encoding: str | None = None,
        encoding_errors: Literal["strict", "replace"] = "replace",
        raw_stdout: bool = False,
        timeout: float | None = None,
        timeout_mode: ProcessTimeoutMode = "total",
    ) -> ProtocolProcessSession:
        """Start a process whose stdout and stderr are consumed separately."""
        record = await self.launch(
            executable,
            arguments,
            cwd=cwd,
            env=env,
            encoding=encoding,
            encoding_errors=encoding_errors,
            timeout=timeout,
            timeout_mode=timeout_mode,
            read_mode="protocol",
            raw_stdout=raw_stdout,
        )
        return ProtocolProcessSession(self, record.process_id)

    async def launch(
        self,
        executable: str | Path,
        arguments: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str] | None,
        encoding: str | None,
        encoding_errors: Literal["strict", "replace"],
        timeout: float | None,
        timeout_mode: ProcessTimeoutMode,
        read_mode: ProcessReadMode,
        raw_stdout: bool,
    ) -> ProcessRecord:
        """Create and begin tracking one subprocess."""
        if timeout is not None and timeout <= 0:
            raise ProcessStartError("Process timeout must be greater than zero.")

        working_directory = (cwd or self.root).resolve()
        if not working_directory.is_dir():
            raise ProcessStartError(f"Process working directory does not exist: {working_directory}")
        try:
            working_directory.relative_to(self.root)
        except ValueError as error:
            raise ProcessStartError(
                f"Process working directory must stay inside the workspace: {working_directory}"
            ) from error

        selected_encoding = encoding or locale.getencoding()
        try:
            codecs.lookup(selected_encoding)
        except LookupError as error:
            raise ProcessStartError(f"Unknown process encoding: {selected_encoding}") from error

        loop = asyncio.get_running_loop()
        process_id = uuid4().hex
        record = ProcessRecord(
            process_id=process_id,
            executable=str(executable),
            arguments=tuple(arguments),
            cwd=working_directory,
            encoding=selected_encoding,
            encoding_errors=encoding_errors,
            read_mode=read_mode,
            raw_stdout=raw_stdout,
            timeout=timeout,
            timeout_mode=timeout_mode,
            started_monotonic=loop.time(),
            last_output_monotonic=loop.time(),
            output_queue=asyncio.Queue(maxsize=self.QUEUE_SIZE),
            stdout_queue=asyncio.Queue(maxsize=self.QUEUE_SIZE),
            stderr_queue=asyncio.Queue(maxsize=self.QUEUE_SIZE),
            stdin_decoder=codecs.getincrementaldecoder(selected_encoding)(errors=encoding_errors),
        )
        self.records[process_id] = record

        child_environment = None
        if env is not None:
            child_environment = os.environ.copy()
            child_environment.update(env)

        try:
            process = await asyncio.create_subprocess_exec(
                record.executable,
                *record.arguments,
                cwd=record.cwd,
                env=child_environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as error:
            record.status = ProcessStatus.FAILED
            record.error = f"{type(error).__name__}: {error}"
            record.finished_at = datetime.now(timezone.utc)
            record.finished_monotonic = loop.time()
            raise ProcessStartError(f"Could not start process {record.executable!r}.") from error

        record.process = process
        record.pid = process.pid
        record.status = ProcessStatus.RUNNING
        record.reader_tasks = [
            asyncio.create_task(self.read_pipe(record, "stdout")),
            asyncio.create_task(self.read_pipe(record, "stderr")),
        ]
        record.wait_task = asyncio.create_task(self.monitor(record))
        if timeout is not None:
            record.timeout_task = asyncio.create_task(self.watch_timeout(record))

        logger.info("Process %s started", record.process_id)
        return record

    async def read_pipe(
        self,
        record: ProcessRecord,
        stream: Literal["stdout", "stderr"],
    ) -> None:
        """Drain one OS pipe, retain decoded text, and publish output chunks."""
        process = cast(asyncio.subprocess.Process, record.process)
        reader = process.stdout if stream == "stdout" else process.stderr
        assert reader is not None
        decoder = codecs.getincrementaldecoder(record.encoding)(errors=record.encoding_errors)
        cancelled = False

        try:
            while raw := await reader.read(self.READ_SIZE):
                record.last_output_monotonic = asyncio.get_running_loop().time()
                text = decoder.decode(raw)
                if "\ufffd" in text:
                    record.had_decoding_errors = True
                if text:
                    self.record_log(record, stream, text)

                if record.read_mode == "combined":
                    if text:
                        assert record.output_queue is not None
                        await record.output_queue.put(ProcessOutput(stream=stream, text=text))
                elif stream == "stdout":
                    assert record.stdout_queue is not None
                    if record.raw_stdout:
                        await record.stdout_queue.put(raw)
                    elif text:
                        await record.stdout_queue.put(text)
                elif text:
                    assert record.stderr_queue is not None
                    await record.stderr_queue.put(text)

            final_text = decoder.decode(b"", final=True)
            if "\ufffd" in final_text:
                record.had_decoding_errors = True
            if final_text:
                self.record_log(record, stream, final_text)
                if record.read_mode == "combined":
                    assert record.output_queue is not None
                    await record.output_queue.put(ProcessOutput(stream=stream, text=final_text))
                elif stream == "stdout" and not record.raw_stdout:
                    assert record.stdout_queue is not None
                    await record.stdout_queue.put(final_text)
                elif stream == "stderr":
                    assert record.stderr_queue is not None
                    await record.stderr_queue.put(final_text)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except UnicodeError as error:
            record.had_decoding_errors = True
            record.error = f"Could not decode {stream} using {record.encoding}: {error}"
            record.status = ProcessStatus.FAILED
            await self.stop_record(record, ProcessStatus.FAILED)
        finally:
            if not cancelled:
                if record.read_mode == "combined":
                    assert record.output_queue is not None
                    await record.output_queue.put(self.EOF)
                elif stream == "stdout":
                    assert record.stdout_queue is not None
                    await record.stdout_queue.put(self.EOF)
                else:
                    assert record.stderr_queue is not None
                    await record.stderr_queue.put(self.EOF)

    async def monitor(self, record: ProcessRecord) -> None:
        """Record terminal state after the operating-system process exits."""
        process = cast(asyncio.subprocess.Process, record.process)
        record.return_code = await process.wait()
        record.finished_at = datetime.now(timezone.utc)
        record.finished_monotonic = asyncio.get_running_loop().time()
        if record.status == ProcessStatus.RUNNING:
            record.status = ProcessStatus.EXITED
        if record.timeout_task is not None and record.status != ProcessStatus.TIMED_OUT:
            record.timeout_task.cancel()
        logger.info("Process %s stopped with return code %s", record.process_id, record.return_code)

    async def watch_timeout(self, record: ProcessRecord) -> None:
        """Apply a total or output-idle timeout to one running process."""
        assert record.timeout is not None
        try:
            while record.status in self.RUNNING_STATUSES:
                reference = (
                    record.started_monotonic
                    if record.timeout_mode == "total"
                    else record.last_output_monotonic
                )
                remaining = record.timeout - (asyncio.get_running_loop().time() - reference)
                if remaining <= 0:
                    break
                await asyncio.sleep(remaining)
            if record.status in self.RUNNING_STATUSES:
                await self.stop_record(record, ProcessStatus.TIMED_OUT)
                logger.warning("Process %s timed out", record.process_id)
        except asyncio.CancelledError:
            return

    async def stop_record(self, record: ProcessRecord, status: ProcessStatus) -> None:
        """Terminate one directly managed asyncio subprocess."""
        async with record.stop_lock:
            process = record.process
            if process is None:
                return
            if process.returncode is None:
                record.status = status
                if process.stdin is not None and not process.stdin.is_closing():
                    process.stdin.close()
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

            if record.wait_task is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(record.wait_task),
                        timeout=self.TERMINATE_GRACE_SECONDS,
                    )
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.shield(record.wait_task)

    async def terminate(self, process_id: str) -> ProcessResult:
        """Terminate a tracked process and return its final state."""
        record = self.record(process_id)
        await self.stop_record(record, ProcessStatus.TERMINATED)
        return self.result(record)

    async def wait(self, process_id: str) -> ProcessResult:
        """Wait for a tracked process, cleaning it up if the caller is cancelled."""
        record = self.record(process_id)
        if record.wait_task is not None:
            try:
                await asyncio.shield(record.wait_task)
            except asyncio.CancelledError:
                await asyncio.shield(self.stop_record(record, ProcessStatus.TERMINATED))
                raise

        return self.result(record)

    async def write_stdin(self, process_id: str, data: str | bytes) -> None:
        """Write text, or protocol bytes, to a tracked process stdin."""
        record = self.record(process_id)
        process = record.process
        if process is None or process.returncode is not None or process.stdin is None:
            raise ProcessExitedError(f"Process {process_id} is not accepting stdin.")
        async with record.stdin_lock:
            if isinstance(data, bytes):
                if not record.raw_stdout:
                    raise ProcessStreamError("Bytes stdin is reserved for raw protocol sessions.")
                encoded = data
                assert record.stdin_decoder is not None
                text = record.stdin_decoder.decode(data)
            else:
                encoded = data.encode(record.encoding, errors=record.encoding_errors)
                text = data

            try:
                process.stdin.write(encoded)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise ProcessExitedError(
                    f"Process {process_id} closed stdin before the write completed."
                ) from error
            if text:
                self.record_log(record, "stdin", text)

    async def close_stdin(self, process_id: str) -> None:
        """Close a tracked process stdin after flushing its transcript decoder."""
        record = self.record(process_id)
        process = record.process
        if process is None or process.stdin is None or process.stdin.is_closing():
            return
        async with record.stdin_lock:
            if record.stdin_decoder is not None:
                final_text = record.stdin_decoder.decode(b"", final=True)
                if final_text:
                    self.record_log(record, "stdin", final_text)
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                return

    async def output(self, process_id: str) -> AsyncIterator[ProcessOutput]:
        """Yield tagged text chunks until both stdout and stderr reach EOF."""
        record = self.record(process_id)
        if record.read_mode != "combined":
            raise ProcessStreamError("output() is unavailable for protocol sessions.")
        if record.output_claimed:
            raise ProcessStreamError("Combined process output can only be consumed once.")
        record.output_claimed = True
        assert record.output_queue is not None

        eof_count = 0
        try:
            while eof_count < 2:
                item = await record.output_queue.get()
                if item is self.EOF:
                    eof_count += 1
                else:
                    yield cast(ProcessOutput, item)
        except asyncio.CancelledError:
            await asyncio.shield(self.stop_record(record, ProcessStatus.TERMINATED))
            raise

    async def read_stdout(self, process_id: str) -> str | bytes | None:
        """Read the next stdout chunk from a protocol session."""
        record = self.record(process_id)
        if record.read_mode != "protocol":
            raise ProcessStreamError("Direct stdout reads require a protocol session.")
        if record.stdout_eof:
            return None
        if record.stdout_reading:
            raise ProcessStreamError("Only one active stdout reader is allowed.")
        record.stdout_reading = True
        try:
            assert record.stdout_queue is not None
            item = await record.stdout_queue.get()
        finally:
            record.stdout_reading = False
        if item is self.EOF:
            record.stdout_eof = True
            return None
        return cast(str | bytes, item)

    async def read_stderr(self, process_id: str) -> str | None:
        """Read the next decoded stderr chunk from a protocol session."""
        record = self.record(process_id)
        if record.read_mode != "protocol":
            raise ProcessStreamError("Direct stderr reads require a protocol session.")
        if record.stderr_eof:
            return None
        if record.stderr_reading:
            raise ProcessStreamError("Only one active stderr reader is allowed.")
        record.stderr_reading = True
        try:
            assert record.stderr_queue is not None
            item = await record.stderr_queue.get()
        finally:
            record.stderr_reading = False
        if item is self.EOF:
            record.stderr_eof = True
            return None
        return cast(str, item)

    def record(self, process_id: str) -> ProcessRecord:
        """Return one tracked process or raise a domain error."""
        try:
            return self.records[process_id]
        except KeyError as error:
            raise ProcessNotFoundError(
                f"No retained process exists for id {process_id!r}."
            ) from error

    def record_log(self, record: ProcessRecord, stream: ProcessStream, text: str) -> None:
        """Append decoded text to a process transcript."""
        record.transcript.append(
            ProcessLogEntry(
                sequence=record.next_sequence,
                timestamp=datetime.now(timezone.utc),
                stream=stream,
                text=text,
            )
        )
        record.next_sequence += 1

    def result(self, record: ProcessRecord) -> ProcessResult:
        """Build the terminal result for a tracked process."""
        end = record.finished_monotonic or asyncio.get_running_loop().time()
        return ProcessResult(
            process_id=record.process_id,
            status=record.status,
            return_code=record.return_code,
            duration=max(0.0, end - record.started_monotonic),
            error=record.error,
        )

    def summary(self, record: ProcessRecord) -> ProcessSummary:
        """Build a model-visible process summary."""
        return ProcessSummary(
            process_id=record.process_id,
            pid=record.pid,
            executable=record.executable,
            arguments=list(record.arguments),
            cwd=str(record.cwd),
            status=record.status,
            started_at=record.started_at,
            finished_at=record.finished_at,
            return_code=record.return_code,
            timeout=record.timeout,
            timeout_mode=record.timeout_mode if record.timeout is not None else None,
            encoding=record.encoding,
            had_decoding_errors=record.had_decoding_errors,
        )

    def overview(self, status: Literal["all", "running", "completed"] = "all") -> ProcessOverview:
        """Return a snapshot of current and completed process state."""
        running = sum(record.status in self.RUNNING_STATUSES for record in self.records.values())
        completed = len(self.records) - running
        selected = list(self.records.values())
        if status == "running":
            selected = [record for record in selected if record.status in self.RUNNING_STATUSES]
        elif status == "completed":
            selected = [record for record in selected if record.status not in self.RUNNING_STATUSES]
        selected.sort(key=lambda record: record.started_at, reverse=True)
        return ProcessOverview(
            running=running,
            completed=completed,
            processes=[self.summary(record) for record in selected],
        )

    def details(self, process_id: str) -> ProcessDetails:
        """Return complete retained state for one tracked process."""
        record = self.record(process_id)
        return ProcessDetails(
            **self.summary(record).model_dump(),
            error=record.error,
            transcript=[
                ProcessTranscriptItem(
                    sequence=entry.sequence,
                    timestamp=entry.timestamp,
                    stream=entry.stream,
                    text=entry.text,
                )
                for entry in record.transcript
            ],
        )

    async def close(self) -> None:
        """Terminate running processes and stop their background readers."""
        running = [record for record in self.records.values() if record.status in self.RUNNING_STATUSES]
        await asyncio.gather(
            *(self.stop_record(record, ProcessStatus.TERMINATED) for record in running),
            return_exceptions=True,
        )
        tasks = [task for record in self.records.values() for task in record.reader_tasks]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        """Register read-only process inspection entrypoints."""
        icon = self.ICON.icon

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[icon],
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        async def processes_overview(
            ctx: Context,
            status: Literal["all", "running", "completed"] = "all",
        ) -> ProcessOverview:
            """Show current and completed external processes managed by ForgeMCP."""
            await ctx.report_progress(1, total=2, message="Reading process state")
            result = self.overview(status)
            await ctx.report_progress(2, total=2, message="Process overview ready")
            return result

        apps.add_html_resource(self.WIDGET.uri, self.WIDGET.content)

        @mcp.resource(self.RESOURCE_URI, mime_type="application/json", icons=[icon])
        def process_details(process_id: str) -> str:
            """Read the retained state and transcript for one process."""
            try:
                return self.details(process_id).model_dump_json(indent=2)
            except ProcessNotFoundError as error:
                raise ResourceNotFoundError(str(error)) from error

        async def process_completion(
            ref: PromptReference | ResourceTemplateReference,
            argument: CompletionArgument,
            context: CompletionContext | None,
        ) -> Completion | None:
            del context
            if (
                not isinstance(ref, ResourceTemplateReference)
                or ref.uri != self.RESOURCE_URI
                or argument.name != "process_id"
            ):
                return None
            return Completion(
                values=[
                    process_id
                    for process_id in self.records
                    if process_id.startswith(argument.value)
                ]
            )

        complete.add_completion(process_completion)


class ProcessSession:
    """Combined-output view of one tracked process."""

    def __init__(self, service: ProcessService, process_id: str) -> None:
        self.service = service
        self.process_id = process_id

    async def __aenter__(self) -> ProcessSession:
        return self

    async def __aexit__(self, exception_type: Any, exception: Any, traceback: Any) -> None:
        record = self.service.record(self.process_id)
        if record.status in self.service.RUNNING_STATUSES:
            await self.service.terminate(self.process_id)

    def output(self) -> AsyncIterator[ProcessOutput]:
        return self.service.output(self.process_id)

    async def write_stdin(self, text: str) -> None:
        await self.service.write_stdin(self.process_id, text)

    async def close_stdin(self) -> None:
        await self.service.close_stdin(self.process_id)

    async def wait(self) -> ProcessResult:
        return await self.service.wait(self.process_id)

    async def terminate(self) -> ProcessResult:
        return await self.service.terminate(self.process_id)


class ProtocolProcessSession:
    """Separate-stream view for line or byte-framed process protocols."""

    def __init__(self, service: ProcessService, process_id: str) -> None:
        self.service = service
        self.process_id = process_id

    async def __aenter__(self) -> ProtocolProcessSession:
        return self

    async def __aexit__(self, exception_type: Any, exception: Any, traceback: Any) -> None:
        record = self.service.record(self.process_id)
        if record.status in self.service.RUNNING_STATUSES:
            await self.service.terminate(self.process_id)

    async def read_stdout(self) -> str | bytes | None:
        try:
            return await self.service.read_stdout(self.process_id)
        except asyncio.CancelledError:
            record = self.service.record(self.process_id)
            await asyncio.shield(
                self.service.stop_record(record, ProcessStatus.TERMINATED)
            )
            raise

    async def read_stderr(self) -> str | None:
        try:
            return await self.service.read_stderr(self.process_id)
        except asyncio.CancelledError:
            record = self.service.record(self.process_id)
            await asyncio.shield(
                self.service.stop_record(record, ProcessStatus.TERMINATED)
            )
            raise

    async def write_stdin(self, data: str | bytes) -> None:
        await self.service.write_stdin(self.process_id, data)

    async def close_stdin(self) -> None:
        await self.service.close_stdin(self.process_id)

    async def wait(self) -> ProcessResult:
        return await self.service.wait(self.process_id)

    async def terminate(self) -> ProcessResult:
        return await self.service.terminate(self.process_id)
