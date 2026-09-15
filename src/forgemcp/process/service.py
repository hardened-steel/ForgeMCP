"""Asynchronous subprocess lifecycle, transcript, and MCP inspection."""

from __future__ import annotations

import asyncio
from asyncio.subprocess import Process
import codecs
import locale
import logging
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field
from forgemcp import markdown

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
    ProcessTimeout,
    ProcessLogEntry,
    ProcessOverview,
    ProcessEncoding,
    ProcessStatus,
    ProcessStream,
    ProcessSummary,
)

logger = logging.getLogger(__name__)


@dataclass
class ProcessRecord:
    """Mutable internal state owned by ProcessService."""

    summary: ProcessSummary
    status: ProcessStatus
    start: float = field(init=False, default=0)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Future[list[None]] | None = field(init=False, default=None)


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


    async def monitor(self, process: Process) -> None:
        loop = asyncio.get_running_loop()
        self.start = loop.time()

        status = self.status
        summary = self.summary
        timeout = summary.timeout
        task = asyncio.create_task(process.wait())
        try:
            while True:
                finished = False
                message: str
                result: Literal["interrupted", "running"] | int

                try:
                    result = await asyncio.wait_for(asyncio.shield(task), 0.250)
                    finished = True
                    message = f"Process {summary.process_id} finished with return code {result}"
                except TimeoutError:
                    pass

                now = loop.time()
                duration = now - self.start

                if not finished:
                    if timeout.total is not None:
                        finished = duration > timeout.total
                        if finished:
                            await self.terminate(process)
                            result = "interrupted"
                            message =  f"Process {summary.process_id} interrupted by timeout {timeout.total}"

                    if not finished and timeout.idle is not None:
                        async with self.lock:
                            finished = now - (status.transcript[-1].time if len(status.transcript) else self.start) > timeout.idle
                        if finished:
                            await self.terminate(process)
                            result = "interrupted"
                            message =  f"Process {summary.process_id} interrupted by idle {timeout.idle}"

                if finished:
                    async with self.lock:
                        self.status.work_time = duration
                        self.status.current_status = result
                    logger.info(message)
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
        queue: asyncio.Queue[str | None]
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
            while True:
                chunk = await reader.read(128)
                if not chunk:
                    break

                text = decoder.decode(chunk)
                await queue.put(text)
                async with self.lock:
                    self.status.transcript.append(ProcessLogEntry(time=loop.time(), stream=stream, text=text))

            if text := decoder.final():
                async with self.lock:
                    self.status.transcript.append(ProcessLogEntry(time=loop.time(), stream=stream, text=text))
                await queue.put(text)

        finally:
            await queue.put(None)


    async def write_stream(
        self,
        writer: asyncio.StreamWriter,
        encoder: ChunkEncoder,
        queue: asyncio.Queue[str | None]
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
            while True:
                text = await queue.get()
                if text is None:
                    writer.write(encoder.final())
                    await writer.drain()
                    break

                writer.write(encoder.encode(text))
                async with self.lock:
                    self.status.transcript.append(ProcessLogEntry(time=loop.time(), stream="stdin", text=text))
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


class ChunkDecoder:
    def __init__(self, encoding: ProcessEncoding):
        selected_encoding = locale.getencoding() if encoding.driver == "default" else encoding.driver
        try:
            codecs.lookup(selected_encoding)
        except LookupError as error:
            raise ProcessStartError(f"Unknown process encoding: {selected_encoding}") from error
        self.decoder = codecs.getincrementaldecoder(encoding.driver)(errors=encoding.errors)


    def decode(self, array: bytes) -> str:
        return self.decoder.decode(array)


    def final(self) -> str:
        return self.decoder.decode(b"", final=True)


class ChunkEncoder:
    def __init__(self, encoding: ProcessEncoding):
        selected_encoding = locale.getencoding() if encoding.driver == "default" else encoding.driver
        try:
            codecs.lookup(selected_encoding)
        except LookupError as error:
            raise ProcessStartError(f"Unknown process encoding: {selected_encoding}") from error
        self.encoder = codecs.getincrementalencoder(encoding.driver)(errors=encoding.errors)


    def encode(self, string: str) -> bytes:
        return self.encoder.encode(string)


    def final(self) -> bytes:
        return self.encoder.encode("", final=True)


class ProcessService:
    """Run external development tools and expose their state without allowing arbitrary execution."""

    WIDGET = Widget("assets/process-overview.html")
    ICON = IconFile("icons/process.svg")
    RESOURCE_URI = "forgemcp://processes/{process_id}"


    def __init__(self, workspace_root: Path) -> None:
        self.id_counter = 0
        self.root = workspace_root.resolve()
        self.records: dict[int, ProcessRecord] = {}


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
        """Start a process whose stdout and stderr are consumed separately."""
        working_directory = (cwd or self.root).resolve()
        if not working_directory.is_dir():
            raise ProcessStartError(f"Process working directory does not exist: {working_directory}")
        try:
            working_directory.relative_to(self.root)
        except ValueError as error:
            raise ProcessStartError(
                f"Process working directory must stay inside the workspace: {working_directory}"
            ) from error

        child_environment = os.environ.copy() if inherit_environment else {}
        child_environment.update(env or {})

        process = await asyncio.create_subprocess_exec(
            executable,
            *arguments,
            cwd=cwd,
            env=child_environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        summary = ProcessSummary(
            process_id=self.id_counter,
            executable=executable,
            arguments=arguments,
            cwd=working_directory,
            encoding=encoding.driver,
            timeout=timeout
        )

        status = ProcessStatus(
            pid=process.pid,
            current_status="running"
        )

        stdin  = asyncio.Queue[str | None]()
        stdout = asyncio.Queue[str | None]()
        stderr = asyncio.Queue[str | None]()
        
        record = ProcessRecord(summary, status)
        self.records[self.id_counter] = record
        self.id_counter += 1

        record.task = asyncio.gather(
            record.read_stream("stdout", process.stdout, ChunkDecoder(encoding), stdout),
            record.read_stream("stderr", process.stderr, ChunkDecoder(encoding), stderr),
            record.write_stream(process.stdin, ChunkEncoder(encoding), stdin),
            record.monitor(process)
        )

        return ProcessSession(process, record, stdin, stdout, stderr)


    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
            """Register read-only process inspection entrypoints."""
            icon = self.ICON.icon
            
            async def get_stream_content(self, process_id: int, stream: ProcessStream) -> str:
                """Get process stream content"""
                record = self.records[process_id]
                result = ""
                async with record.lock:
                    for log_entry in record.status.transcript:
                        if log_entry.stream == stream:
                            result += log_entry.text
                return result

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
                records = self.records.items()
                completed = 0
                running = 0
                processes = [ProcessSummary]
                for i, item in enumerate(records):
                    await ctx.report_progress(i, total=len(records), message=f"Reading process state: {i}")
                    _, record = item
                    async with record.lock:
                        if record.status.current_status == "running":
                            runnings += 1
                            if status == "all" or status == "running":
                                processes.append(record.summary)
                        else:
                            completed += 1
                            if status == "all" or status == "completed":
                                processes.append(record.summary)

                return ProcessOverview(running=running, completed=completed, processes=processes)
    
            apps.add_html_resource(self.WIDGET.uri, self.WIDGET.content)

            @mcp.resource(self.RESOURCE_URI, mime_type="text/markdown", icons=[icon])
            async def process_details(process_id: int) -> str:
                """Read the retained state and transcript for one process."""
                if process_id not in self.records:
                    raise ResourceNotFoundError()

                record = self.records[process_id]
                async with record.lock:
                    document = markdown.Document()

                    document.add(markdown.Heading(record.summary.executable))
                    document.add(markdown.Paragraph("arguments"))
                    document.add(markdown.OrderedList(record.summary.arguments))

                    table = markdown.Table(["Settings", "Values"])
                    table.add(["id", record.summary.process_id])
                    table.add(["state", str(record.status.current_status)])
                    table.add(["encoding", record.summary.encoding])
                    document.add(table)

                    document.add(markdown.Heading("process log", level=2))
                    table = markdown.Table(["timestamp", "direction", "text"])
                    for log_entry in record.status.transcript:
                        table.add([log_entry.timestamp, log_entry.stream, log_entry.timestamp])
                    document.add(table)

                return document.render()
    
            async def process_completion(
                ref: PromptReference | ResourceTemplateReference,
                argument: CompletionArgument,
                context: CompletionContext | None,
            ) -> Completion | None:
                return None


@dataclass
class ProcessSession:
    """Combined-output view of one tracked process."""
    process: Process
    record: ProcessRecord
    stdin: asyncio.Queue[str | None]
    stdout: asyncio.Queue[str | None]
    stderr: asyncio.Queue[str | None]


    async def __aenter__(self) -> ProcessSession:
        return self

    async def __aexit__(self, exception_type: Any, exception: Any, traceback: Any) -> None:
        await self.record.terminate(self.process)

    #def output(self) -> AsyncIterator[ProcessOutput]:
    #    return self.service.output(self.process_id)

    async def write_stdin(self, text: str) -> None:
        await self.service.write_stdin(self.process_id, text)

    async def close_stdin(self) -> None:
        await self.service.close_stdin(self.process_id)

    #async def wait(self) -> ProcessResult:
    #    return await self.service.wait(self.process_id)

    #async def terminate(self) -> ProcessResult:
    #    return await self.service.terminate(self.process_id)
