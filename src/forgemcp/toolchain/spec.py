"""Platform-independent tool, parser, and managed execution contracts."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol

from forgemcp.process.errors import ProcessError
from forgemcp.process.models import ProcessResult, ProcessStatus
from forgemcp.process.service import ProcessService

from .errors import ToolCommandError, ToolParserError


class ToolKind(StrEnum):
    BUILD_SYSTEM = "build_system"
    BUILD_RUNNER = "build_runner"
    TEST_RUNNER = "test_runner"
    COMPILER = "compiler"
    LINKER = "linker"
    LANGUAGE_SERVER = "language_server"
    VERSION_CONTROL = "version_control"
    DEBUGGER = "debugger"
    OTHER = "other"


@dataclass(frozen=True)
class ToolOutput:
    stream: Literal["stdout", "stderr"]
    text: str


EventEmitter = Callable[[object], Awaitable[None]]


class ToolParser(Protocol):
    async def __call__(
        self, output: AsyncIterator[ToolOutput], emit: EventEmitter,
    ) -> object: ...


def version_parser(pattern: str) -> ToolParser:
    """Build a bounded incremental version parser shared by built-in modules."""
    expression = re.compile(pattern, re.IGNORECASE)

    async def parse(output: AsyncIterator[ToolOutput], emit: EventEmitter) -> str | None:
        tails = {"stdout": "", "stderr": ""}
        version = None
        async for chunk in output:
            text = tails[chunk.stream] + chunk.text
            # Retain only an unfinished line; don't copy the process transcript.
            lines = text.splitlines(keepends=True)
            tail = ""
            for line in lines:
                if line.endswith(("\n", "\r")):
                    if version is None and (match := expression.search(line)):
                        version = match.group(1)
                else:
                    tail = line[-4096:]
            tails[chunk.stream] = tail
        for tail in tails.values():
            if version is None and (match := expression.search(tail)):
                version = match.group(1)
        return version

    return parse


@dataclass(frozen=True)
class ToolCommand:
    arguments: Callable[..., tuple[str, ...]]
    parser: str | None = None
    shell: bool = False
    encoding: str | None = None
    timeout: float | None = 15.0
    success_codes: frozenset[int] = frozenset({0})
    path: Path | None = field(default=None, repr=False)
    processes: ProcessService | None = field(default=None, repr=False, compare=False)
    environment: Mapping[str, str] | None = field(default=None, repr=False)
    inherit_environment: bool = True
    parse: ToolParser | None = field(default=None, repr=False, compare=False)
    label: str = "unbound command"

    def __call__(self, **arguments: object) -> CommandExecution:
        if self.path is None or self.processes is None:
            raise ToolCommandError(f"Cannot execute {self.label} before binding.")
        try:
            argv = self.arguments(**arguments)
            if not isinstance(argv, tuple) or not all(isinstance(arg, str) for arg in argv):
                raise TypeError("Argument builder must return tuple[str, ...].")
        except Exception:
            raise ToolCommandError(f"Invalid arguments for {self.label}.") from None
        return CommandExecution(self, argv)


class CommandExecution:
    """One lazy execution; live events have one consumer and results are retained.

    Events emitted without an active events consumer are discarded. They are not a
    second transcript. Start iterating before awaiting the result to receive events.
    """

    END = object()

    def __init__(self, command: ToolCommand, arguments: tuple[str, ...]) -> None:
        self.command = command
        self.arguments = arguments
        self.task: asyncio.Task[object] | None = None
        self.queue: asyncio.Queue[object] = asyncio.Queue(maxsize=128)
        self.events_claimed = False
        self.events_active = False
        self.parsed_result: object = None
        self.process_result: ProcessResult | None = None

    def __await__(self):
        return self.result().__await__()

    def __aiter__(self) -> AsyncIterator[object]:
        return self.events()

    def start(self) -> asyncio.Task[object]:
        if self.task is None:
            self.task = asyncio.create_task(self.run())
        return self.task

    async def emit(self, event: object) -> None:
        if self.events_active:
            await self.queue.put(event)

    async def events(self) -> AsyncIterator[object]:
        if self.events_claimed:
            raise ToolCommandError(f"Events for {self.command.label} have already been consumed.")
        self.events_claimed = True
        self.events_active = True
        task = self.start()
        try:
            while not task.done() or not self.queue.empty():
                item = await self.queue.get()
                if item is self.END:
                    break
                yield item
            await task
        finally:
            self.events_active = False
            while not self.queue.empty():
                self.queue.get_nowait()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def result(self) -> object:
        return await self.start()

    async def run(self) -> object:
        command = self.command
        assert command.processes is not None and command.path is not None
        session = None
        try:
            session = await command.processes.start(
                command.path, self.arguments, shell=command.shell,
                env=command.environment, inherit_environment=command.inherit_environment,
                encoding=command.encoding, timeout=command.timeout,
            )
            await session.close_stdin()
            stream = session.output()

            async def output() -> AsyncIterator[ToolOutput]:
                async for chunk in stream:
                    yield ToolOutput(chunk.stream, chunk.text)

            try:
                if command.parse is not None:
                    self.parsed_result = await command.parse(output(), self.emit)
            except Exception:
                await session.terminate()
                async for _ in stream:
                    pass
                self.process_result = await session.wait()
                raise ToolParserError(f"Parser failed for {command.label}.") from None
            # Parsers may return early; always drain both pipes and check exit status.
            async for _ in stream:
                pass
            self.process_result = await session.wait()
            if (self.process_result.status != ProcessStatus.EXITED
                    or self.process_result.return_code not in command.success_codes):
                raise ToolCommandError(
                    f"{command.label} failed (process {self.process_result.process_id}, "
                    f"status {self.process_result.status}, exit {self.process_result.return_code})."
                )
            return self.parsed_result
        except asyncio.CancelledError:
            if session is not None:
                self.process_result = await asyncio.shield(session.terminate())
            raise
        except ProcessError:
            if session is not None:
                self.process_result = await session.terminate()
            raise ToolCommandError(f"Process execution failed for {command.label}.") from None
        finally:
            if self.events_active:
                # A full queue already wakes the consumer, which also checks task
                # completion. Never block process cancellation on an end marker.
                try:
                    self.queue.put_nowait(self.END)
                except asyncio.QueueFull:
                    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: ToolKind
    path: Path | None
    version: str | None
    commands: Mapping[str, ToolCommand]
    parsers: Mapping[str, ToolParser]

    def __post_init__(self) -> None:
        object.__setattr__(self, "commands", MappingProxyType(dict(self.commands)))
        object.__setattr__(self, "parsers", MappingProxyType(dict(self.parsers)))

    def bind(
        self, *, path: Path, processes: ProcessService,
        environment: Mapping[str, str] | None = None, inherit_environment: bool = True,
    ) -> ToolSpec:
        resolved = path.resolve()
        environment = MappingProxyType(dict(environment)) if environment is not None else None
        commands = {}
        for name, command in self.commands.items():
            if command.parser is not None and command.parser not in self.parsers:
                raise ToolCommandError(f"Unknown parser for {self.name}.{name}.")
            commands[name] = replace(
                command, path=resolved, processes=processes, environment=environment,
                inherit_environment=inherit_environment,
                parse=self.parsers.get(command.parser) if command.parser is not None else None,
                label=f"{self.name}.{name}",
            )
        return replace(self, path=resolved, commands=commands)


@dataclass(frozen=True)
class Toolset:
    id: str
    name: str
    tools: tuple[ToolSpec, ...]
    environment: Mapping[str, str] | None
    inherit_environment: bool

    def __post_init__(self) -> None:
        if any(tool.path is None for tool in self.tools):
            raise ToolCommandError("Toolsets require bound tools.")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ToolCommandError("Toolset contains duplicate tool names.")
        object.__setattr__(self, "tools", tuple(sorted(self.tools, key=lambda tool: tool.name)))
        if self.environment is not None:
            object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))
