"""Asynchronous subprocess lifecycle, transcript, and MCP inspection."""

from __future__ import annotations

import asyncio
from asyncio.subprocess import Process
import codecs
import locale
import logging
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from forgemcp import markdown

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
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
from forgemcp.progress import progress

from .errors import (
    ProcessError,
    ProcessExitedError,
    ProcessStartError,
    ProcessStreamError,
)
from .models import (
    ProcessTimeout,
    ProcessLogEntry,
    ProcessOverview,
    ProcessEncoding,
    ProcessStatus,
    ProcessOutput,
    ProcessSummary,
    ProcessInfo,
    ProcessDetails,
)

logger = logging.getLogger(__name__)


@dataclass
class ProcessRecord:
    """Mutable internal state owned by ProcessService."""

    summary: ProcessSummary
    status: ProcessStatus
    start: float = field(init=False, default=0)
    transcript: list[ProcessLogEntry] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def snapshot(self) -> ProcessInfo:
        """Copy the small process state while the caller holds the record lock."""
        return ProcessInfo(
            summary=self.summary,
            status=self.status.model_copy(),
        )

    async def terminate(self, process: Process) -> None:
        try:
            process.terminate()
            try:
                async with asyncio.timeout(2):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
        except ProcessLookupError:
            await process.wait()

    async def monitor(self, process: Process, stopping: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()

        summary = self.summary
        timeout = summary.timeout
        task = asyncio.create_task(process.wait())
        try:
            while True:
                finished = False
                timed_out = False
                message: str
                result: Literal["interrupted", "running", "stopped"] | int

                try:
                    result = await asyncio.wait_for(asyncio.shield(task), 0.250)
                    finished = True
                    message = f"Process {summary.process_id} finished with return code {result}"
                except TimeoutError:
                    pass

                now = loop.time()
                duration = now - self.start

                if not finished:
                    if stopping.is_set():
                        await self.terminate(process)
                        finished = True
                        result = "stopped"
                        message = f"Process {summary.process_id} stopped"
                    elif timeout.total is not None:
                        finished = duration > timeout.total
                        if finished:
                            timed_out = True
                            await self.terminate(process)
                            result = "interrupted"
                            message = f"Process {summary.process_id} interrupted by timeout {timeout.total}"

                    if not finished and timeout.idle is not None:
                        async with self.lock:
                            finished = (
                                now
                                - (
                                    self.transcript[-1].time
                                    if self.transcript
                                    else self.start
                                )
                                > timeout.idle
                            )
                        if finished:
                            timed_out = True
                            await self.terminate(process)
                            result = "interrupted"
                            message = f"Process {summary.process_id} interrupted by idle {timeout.idle}"

                if finished:
                    async with self.lock:
                        self.status.work_time = duration
                        self.status.current_status = result
                    logger.info(message)
                    if timed_out:
                        raise ProcessError(message)
                    break
                else:
                    async with self.lock:
                        self.status.work_time = duration
        finally:
            if task.cancel():
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def read_stream(
        self,
        stream: Literal["stdout", "stderr"],
        reader: asyncio.StreamReader,
        decoder: ChunkDecoder,
        queue: asyncio.Queue[ProcessOutput | None],
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break

                text = decoder.decode(chunk)
                await queue.put(ProcessOutput(stream=stream, text=text))
                async with self.lock:
                    now = loop.time()
                    self.transcript.append(
                        ProcessLogEntry(
                            time=now,
                            stream=stream,
                            text=text,
                        )
                    )

            if text := decoder.final():
                async with self.lock:
                    now = loop.time()
                    self.transcript.append(
                        ProcessLogEntry(
                            time=now,
                            stream=stream,
                            text=text,
                        )
                    )
                await queue.put(ProcessOutput(stream=stream, text=text))

        except (OSError, UnicodeError) as error:
            raise ProcessStreamError(
                f"Cannot read process {self.summary.process_id} {stream}."
            ) from error
        else:
            queue.put_nowait(None)

    @staticmethod
    async def drain_stream(reader: asyncio.StreamReader) -> None:
        """Discard unread pipe data while cleaning up a failed session."""
        while await reader.read(64 * 1024):
            pass

    async def write_stream(
        self,
        writer: asyncio.StreamWriter,
        encoder: ChunkEncoder,
        queue: asyncio.Queue[str | None],
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
            while True:
                text = await queue.get()
                if text is None:
                    writer.write(encoder.final())
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    break

                writer.write(encoder.encode(text))
                async with self.lock:
                    now = loop.time()
                    self.transcript.append(
                        ProcessLogEntry(
                            time=now,
                            stream="stdin",
                            text=text,
                        )
                    )
                await writer.drain()
        except (OSError, UnicodeError) as error:
            raise ProcessStreamError(
                f"Cannot write process {self.summary.process_id} stdin."
            ) from error
        finally:
            # Normal EOF already flushed above; cancellation must not wait for
            # a child that has stopped consuming buffered stdin.
            writer.transport.abort()
            with suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()


class ChunkDecoder:
    def __init__(self, encoding: ProcessEncoding):
        selected_encoding = (
            locale.getencoding() if encoding.driver == "default" else encoding.driver
        )
        try:
            codecs.lookup(selected_encoding)
        except LookupError as error:
            raise ProcessStartError(
                f"Unknown process encoding: {selected_encoding}"
            ) from error
        self.decoder = codecs.getincrementaldecoder(selected_encoding)(
            errors=encoding.errors
        )

    def decode(self, array: bytes) -> str:
        return self.decoder.decode(array)

    def final(self) -> str:
        return self.decoder.decode(b"", final=True)


class ChunkEncoder:
    def __init__(self, encoding: ProcessEncoding):
        selected_encoding = (
            locale.getencoding() if encoding.driver == "default" else encoding.driver
        )
        try:
            codecs.lookup(selected_encoding)
        except LookupError as error:
            raise ProcessStartError(
                f"Unknown process encoding: {selected_encoding}"
            ) from error
        self.encoder = codecs.getincrementalencoder(selected_encoding)(
            errors=encoding.errors
        )

    def encode(self, string: str) -> bytes:
        return self.encoder.encode(string)

    def final(self) -> bytes:
        return self.encoder.encode("", final=True)


class ProcessService:
    """Run external development tools and expose their state without allowing arbitrary execution."""

    WIDGET = Widget("assets/process-overview.html")
    DETAILS_WIDGET = Widget("assets/process-details.html")
    ICON = IconFile("icons/process.svg")
    RESOURCE_URI = "forgemcp://processes/{process_id}"
    OVERVIEW_URI = "forgemcp://processes"

    @staticmethod
    def format_timestamp(value: datetime) -> str:
        """Render a process timestamp to readable UTC seconds."""
        return value.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M:%S UTC")

    @staticmethod
    def describe_status(status: str | int) -> str:
        """Render terminal state in Markdown resources."""
        if isinstance(status, int):
            return (
                "Completed successfully" if status == 0 else f"Exited with code {status}"
            )
        return {
            "running": "Running",
            "interrupted": "Interrupted by timeout",
            "stopped": "Stopped",
            "stream_failure": "Stream failure",
        }[status]

    def __init__(
        self,
        workspace_root: Path,
        *,
        allowed_roots: Sequence[Path] = (),
        progress_interval: float = 1.0,
    ) -> None:
        self.progress_interval = progress_interval
        self.id_counter = 0
        self.root = workspace_root.resolve()
        self.allowed_roots = (self.root, *(path.resolve() for path in allowed_roots))
        self.records: dict[int, ProcessRecord] = {}
        self.sessions: dict[int, ProcessSession] = {}

    async def launch(
        self,
        executable: str | Path,
        arguments: Sequence[str] = (),
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        inherit_environment: bool = True,
        encoding: ProcessEncoding = ProcessEncoding('default'),
        timeout: ProcessTimeout = ProcessTimeout(),
    ) -> ProcessSession:
        """Start a process; manage its streams with async with await service.launch(...)."""
        working_directory = (cwd or self.root).resolve()
        if not working_directory.is_dir():
            raise ProcessStartError(
                f"Process working directory does not exist: {working_directory}"
            )
        if not any(
            working_directory.is_relative_to(root) for root in self.allowed_roots
        ):
            raise ProcessStartError(
                f"Process working directory must stay inside the workspace or configured storage: {working_directory}"
            )

        child_environment = os.environ.copy() if inherit_environment else {}
        child_environment.update(env or {})

        summary = ProcessSummary(
            process_id=self.id_counter,
            executable=str(executable),
            arguments=list(arguments),
            cwd=str(working_directory),
            encoding=encoding.driver,
            timeout=timeout,
        )
        stdout_decoder = ChunkDecoder(encoding)
        stderr_decoder = ChunkDecoder(encoding)
        stdin_encoder = ChunkEncoder(encoding)
        self.id_counter += 1

        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                cwd=working_directory,
                env=child_environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            raise ProcessStartError(f"Cannot start executable {executable}.") from error

        status = ProcessStatus(pid=process.pid, current_status="running")
        stdin = asyncio.Queue[str | None]()
        output = asyncio.Queue[ProcessOutput | None]()
        record = ProcessRecord(summary, status)
        self.records[summary.process_id] = record
        session = ProcessSession(
            process,
            record,
            stdin,
            output,
            stdout_decoder,
            stderr_decoder,
            stdin_encoder,
        )
        session.task = asyncio.create_task(session.run())
        self.sessions[summary.process_id] = session
        session.task.add_done_callback(
            lambda _: self.sessions.pop(summary.process_id, None)
        )
        return session

    async def close(self) -> None:
        """Close all remaining sessions during server shutdown."""
        async with asyncio.TaskGroup() as group:
            for session in tuple(self.sessions.values()):
                group.create_task(session.close())

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
            report_progress = progress(ctx, interval=self.progress_interval)
            records = tuple(self.records.values())
            completed = 0
            running = 0
            processes: list[ProcessInfo] = []
            for i, record in enumerate(records):
                await report_progress(
                    i,
                    total=len(records),
                    message=f"Reading process state: {i}",
                )
                async with record.lock:
                    snapshot = record.snapshot()
                    if snapshot.status.current_status == "running":
                        running += 1
                        if status == "all" or status == "running":
                            processes.append(snapshot)
                    else:
                        completed += 1
                        if status == "all" or status == "completed":
                            processes.append(snapshot)

            await report_progress(
                len(records),
                total=len(records),
                message="Process state ready",
            )
            return ProcessOverview(
                running=running,
                completed=completed,
                processes=processes,
            )

        @apps.tool(
            resource_uri=self.DETAILS_WIDGET.uri,
            icons=[icon],
            annotations=ToolAnnotations(
                read_only_hint=True,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
        )
        async def process_get(ctx: Context, process_id: int) -> ProcessDetails:
            """Show one retained process and its ordered stdin/stdout/stderr transcript."""
            report_progress = progress(ctx, interval=self.progress_interval)
            await report_progress(0, total=1, message="Reading process transcript")
            record = self.records.get(process_id)
            if record is None:
                raise ToolError(f"Process {process_id} is not retained.")
            async with record.lock:
                details = ProcessDetails(
                    process=record.snapshot(),
                    transcript=list(record.transcript),
                )
            await report_progress(1, total=1, message="Process transcript ready")
            return details

        apps.add_html_resource(self.WIDGET.uri, self.WIDGET.content)
        apps.add_html_resource(self.DETAILS_WIDGET.uri, self.DETAILS_WIDGET.content)

        @mcp.resource(self.OVERVIEW_URI, mime_type="text/markdown", icons=[icon])
        async def processes_list() -> str:
            """Read the current retained process list as Markdown."""
            document = markdown.Document([markdown.Heading("Processes")])
            table = markdown.Table(
                ["ID", "Executable", "PID", "Started (UTC)", "Outcome"]
            )
            for record in tuple(self.records.values()):
                async with record.lock:
                    item = record.snapshot()
                status = item.status.current_status
                outcome = self.describe_status(status)
                table.add(
                    [
                        markdown.Link(
                            str(item.summary.process_id),
                            f"forgemcp://processes/{item.summary.process_id}",
                        ),
                        item.summary.executable,
                        str(item.status.pid),
                        self.format_timestamp(item.status.started),
                        outcome,
                    ]
                )
            document.add(table)
            return document.render()

        @mcp.resource(self.RESOURCE_URI, mime_type="text/markdown", icons=[icon])
        async def process_details(process_id: int) -> str:
            """Read the retained state and transcript for one process."""
            if process_id not in self.records:
                raise ResourceNotFoundError()

            record = self.records[process_id]
            async with record.lock:
                item = record.snapshot()
                transcript = list(record.transcript)
            document = markdown.Document(
                [markdown.Heading(f"Process {item.summary.process_id}: {item.summary.executable}")]
            )
            table = markdown.Table(["Property", "Value"])
            table.add(["Outcome", self.describe_status(item.status.current_status)])
            table.add(["PID", str(item.status.pid)])
            table.add(["Started (UTC)", self.format_timestamp(item.status.started)])
            table.add(["Work time", f"{item.status.work_time:.2f} s"])
            table.add(["Working directory", item.summary.cwd])
            table.add(["Encoding", item.summary.encoding])
            table.add(
                [
                    "Total timeout",
                    f"{item.summary.timeout.total} s"
                    if item.summary.timeout.total is not None
                    else "None",
                ]
            )
            table.add(
                [
                    "Idle timeout",
                    f"{item.summary.timeout.idle} s"
                    if item.summary.timeout.idle is not None
                    else "None",
                ]
            )
            document.add(table)
            document.add(markdown.Heading("Arguments", level=2))
            document.add(
                markdown.OrderedList(item.summary.arguments)
                if item.summary.arguments
                else markdown.Paragraph("None")
            )
            document.add(markdown.Heading("Transcript", level=2))
            if not transcript:
                document.add(markdown.Paragraph("No stream chunks recorded."))
            for entry in transcript:
                elapsed = max(
                    0.0,
                    (entry.timestamp - item.status.started).total_seconds(),
                )
                document.add(
                    markdown.Heading(
                        f"{entry.stream} · {self.format_timestamp(entry.timestamp)} · +{elapsed:.3f} s",
                        level=3,
                    )
                )
                document.add(markdown.CodeBlock(entry.text))
            return document.render()

        async def process_completion(
            ref: PromptReference | ResourceTemplateReference,
            argument: CompletionArgument,
            context: CompletionContext | None,
        ) -> Completion | None:
            if (
                not isinstance(ref, ResourceTemplateReference)
                or ref.uri != self.RESOURCE_URI
                or argument.name != "process_id"
            ):
                return None
            values = [
                str(process_id)
                for process_id in self.records
                if str(process_id).startswith(argument.value)
            ]
            return Completion(
                values=values[:100],
                total=len(values),
                has_more=len(values) > 100,
            )

        complete.add_completion(process_completion)


@dataclass
class ProcessSession:
    """Own one process and its stream tasks for an async context."""

    process: Process
    record: ProcessRecord
    stdin: asyncio.Queue[str | None]
    output_queue: asyncio.Queue[ProcessOutput | None]
    stdout_decoder: ChunkDecoder
    stderr_decoder: ChunkDecoder
    stdin_encoder: ChunkEncoder
    task: asyncio.Task[None] | None = field(init=False, default=None)
    stopping: asyncio.Event = field(init=False, default_factory=asyncio.Event)
    stdin_closed: bool = field(init=False, default=False)
    output_claimed: bool = field(init=False, default=False)
    entered: bool = field(init=False, default=False)
    failure: Exception | None = field(init=False, default=None)

    @property
    def process_id(self) -> int:
        return self.record.summary.process_id

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    async def __aenter__(self) -> ProcessSession:
        if self.entered or self.stopping.is_set():
            raise ProcessStreamError("A process session can only be entered once.")
        self.entered = True
        return self

    async def __aexit__(
        self,
        exception_type: Any,
        exception: Any,
        traceback: Any,
    ) -> None:
        await self.close()
        if exception is None and self.failure is not None:
            raise self.failure

    async def run(self) -> None:
        """Own the workers and finish cleanup before publishing session failure."""
        self.record.start = asyncio.get_running_loop().time()
        stdout = cast(asyncio.StreamReader, self.process.stdout)
        stderr = cast(asyncio.StreamReader, self.process.stderr)
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(
                    self.record.read_stream(
                        "stdout",
                        stdout,
                        self.stdout_decoder,
                        self.output_queue,
                    )
                )
                group.create_task(
                    self.record.read_stream(
                        "stderr",
                        stderr,
                        self.stderr_decoder,
                        self.output_queue,
                    )
                )
                writer = group.create_task(
                    self.record.write_stream(
                        cast(asyncio.StreamWriter, self.process.stdin),
                        self.stdin_encoder,
                        self.stdin,
                    )
                )
                monitor = group.create_task(
                    self.record.monitor(self.process, self.stopping)
                )
                await monitor
                writer.cancel()
        except Exception as error:
            while isinstance(error, ExceptionGroup) and len(error.exceptions) == 1:
                error = error.exceptions[0]
            self.failure = error
        finally:
            self.stdin_closed = True
            # Workers have finished before cleanup reads the same pipes.
            try:
                if (
                    self.process.returncode is None
                    or not stdout.at_eof()
                    or not stderr.at_eof()
                ):
                    async with asyncio.TaskGroup() as cleanup:
                        cleanup.create_task(self.record.drain_stream(stdout))
                        cleanup.create_task(self.record.drain_stream(stderr))
                        await self.record.terminate(self.process)
            except Exception as error:
                if self.failure is None:
                    self.failure = error
            finally:
                async with self.record.lock:
                    if self.record.status.current_status == "running":
                        self.record.status.current_status = (
                            "stream_failure"
                            if self.failure is not None
                            else "stopped"
                            if self.stopping.is_set()
                            else cast(int, self.process.returncode)
                        )
                    self.record.status.work_time = (
                        asyncio.get_running_loop().time() - self.record.start
                    )
                # Wake output() even when a reader failed without reaching EOF.
                self.output_queue.put_nowait(None)

    async def close(self) -> None:
        """Stop the process and wait for every worker, including during cancellation."""
        self.stopping.set()
        if self.task is None:
            return
        cancelled = False
        while not self.task.done():
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def output(self) -> AsyncIterator[ProcessOutput]:
        """Consume tagged text once, ending after both pipes reach EOF."""
        if self.output_claimed:
            raise ProcessStreamError("Process output has already been claimed.")
        self.output_claimed = True
        finished = 0
        while finished < 2:
            item = await self.output_queue.get()
            if self.failure is not None:
                await self.wait()
            if item is None:
                finished += 1
                if self.task is not None and self.task.done():
                    await self.wait()
            else:
                yield item
        if self.failure is not None:
            await self.wait()

    async def write_stdin(self, text: str) -> None:
        """Queue text for the stdin writer without waiting for transport drain."""
        if (
            self.process.returncode is not None
            or self.stopping.is_set()
            or self.stdin_closed
        ):
            raise ProcessExitedError("Process stdin is closed.")
        await self.stdin.put(text)

    async def close_stdin(self) -> None:
        """Queue EOF after prior writes; repeated calls are harmless."""
        if self.stdin_closed:
            return
        self.stdin_closed = True
        if self.process.returncode is not None or self.stopping.is_set():
            return
        await self.stdin.put(None)

    async def wait(self) -> int:
        """Wait for process exit and pipe readers; return the same exit code on every call."""
        if self.task is None:
            raise ProcessStreamError("Process session has not been started.")
        await asyncio.shield(self.task)
        if self.failure is not None:
            raise self.failure
        return cast(int, self.process.returncode)
