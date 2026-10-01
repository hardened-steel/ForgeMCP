"""Typed methods for cmake."""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, TypedDict

from pydantic import BaseModel, Field

from forgemcp.process.errors import ProcessError
from forgemcp.process.models import ProcessTimeout
from forgemcp.process.service import ProcessService

from ..errors import ToolCommandError, ToolParserError
from ..spec import ToolInfo, ToolKind, ToolSpec


type Progress = Callable[[str], Awaitable[None]]


class PresetsResult(BaseModel):
    configure: list[str] = Field(default_factory=list)
    build: list[str] = Field(default_factory=list)
    test: list[str] = Field(default_factory=list)


class ConfigureResult(BaseModel):
    return_code: int = 0
    process_id: int | None = None
    configured: bool = False
    generated: bool = False


class BuildResult(BaseModel):
    return_code: int = 0
    process_id: int | None = None
    completed_steps: int | None = None
    total_steps: int | None = None


class Presets(Protocol):
    async def __call__(self, source: Path) -> PresetsResult: ...


class Configure(Protocol):
    async def __call__(
        self,
        source: Path,
        build_directory: Path | None,
        *,
        preset: str | None = None,
        generator: str | None = None,
        definitions: Mapping[str, str] | None = None,
        timeout: ProcessTimeout = ProcessTimeout(total=600),
        on_progress: Progress | None = None,
    ) -> ConfigureResult: ...


class Build(Protocol):
    async def __call__(
        self,
        source: Path,
        build_directory: Path | None,
        *,
        preset: str | None = None,
        configuration: str | None = None,
        targets: Sequence[str] | None = None,
        parallel: int | None = None,
        timeout: ProcessTimeout = ProcessTimeout(total=600),
        on_progress: Progress | None = None,
    ) -> BuildResult: ...


class Methods(TypedDict):
    version: Callable[[], Awaitable[str]]
    presets: Presets
    configure: Configure
    build: Build


def parse_cache(text: str) -> dict[str, str]:
    entries = {}
    for line in text.splitlines():
        if line.startswith(("//", "#")):
            continue
        key_type, separator, value = line.partition("=")
        key, colon, _ = key_type.partition(":")
        if separator and colon:
            entries[key] = value
    return entries


def create_spec(
    path: Path,
    processes: ProcessService,
    environment: Mapping[str, str] | None = None,
    inherit_environment: bool = True,
) -> ToolSpec:
    path = path.resolve()

    async def version() -> str:
        output = {"stdout": "", "stderr": ""}
        try:
            async with await processes.launch(
                path,
                ('--version',),
                env=environment,
                inherit_environment=inherit_environment,
                timeout=ProcessTimeout(total=15),
            ) as session:
                await session.close_stdin()
                async for chunk in session.output():
                    # Only the short version banner is needed; drain the rest.
                    remaining = 4096 - len(output[chunk.stream])
                    output[chunk.stream] += chunk.text[:remaining]
                code = await session.wait()
                if code != 0:
                    raise ToolCommandError(f"{INFO.name}.version failed (exit {code}).")
        except ProcessError as error:
            raise ToolCommandError(f"Cannot read {INFO.name} version.") from error
        for text in output.values():
            if match := re.search(
                'cmake version ([0-9][0-9A-Za-z.+~-]*)',
                text,
                re.IGNORECASE | re.MULTILINE,
            ):
                return match.group(1)
        raise ToolParserError(f"Cannot parse {INFO.name} version.")

    async def execute(
        source: Path,
        arguments: Sequence[str],
        timeout: ProcessTimeout,
        on_line: Progress,
    ) -> tuple[int, int]:
        buffers = {"stdout": "", "stderr": ""}
        session = None
        try:
            async with await processes.launch(
                path,
                arguments,
                cwd=source,
                env=environment,
                inherit_environment=inherit_environment,
                timeout=timeout,
            ) as session:
                await session.close_stdin()
                async for chunk in session.output():
                    buffers[chunk.stream] += chunk.text.replace("\r", "\n")
                    while "\n" in buffers[chunk.stream]:
                        line, _, buffers[chunk.stream] = buffers[chunk.stream].partition("\n")
                        if line.strip():
                            await on_line(line)
                for line in buffers.values():
                    if line.strip():
                        await on_line(line)
                return await session.wait(), session.process_id
        except ProcessError as error:
            raise ToolCommandError(
                str(error),
                process_id=session.process_id if session is not None else None,
            ) from error

    async def presets(source: Path) -> PresetsResult:
        result = PresetsResult()
        kind = None

        async def parse(line: str) -> None:
            nonlocal kind
            heading = re.fullmatch(r"Available (\w+) presets:", line.strip())
            if heading:
                kind = heading.group(1)
            elif kind in ("configure", "build", "test"):
                match = re.fullmatch(r'\s+"(.*?)"(?:\s+-.*)?', line)
                if match is not None:
                    getattr(result, kind).append(match.group(1))

        code, process_id = await execute(
            source,
            ("--list-presets=all",),
            ProcessTimeout(total=30),
            parse,
        )
        if code != 0:
            raise ToolCommandError(f"CMake could not list presets (exit {code}, process {process_id}).")
        return result

    async def configure(
        source: Path,
        build_directory: Path | None,
        *,
        preset: str | None = None,
        generator: str | None = None,
        definitions: Mapping[str, str] | None = None,
        timeout: ProcessTimeout = ProcessTimeout(total=600),
        on_progress: Progress | None = None,
    ) -> ConfigureResult:
        arguments = ["--preset", preset] if preset is not None else ["-S", str(source)]
        if preset is None and build_directory is None:
            raise ToolCommandError("A plain configure requires a build directory.")
        if build_directory is not None:
            arguments.extend(("-B", str(build_directory)))
        if generator is not None:
            arguments.extend(("-G", generator))
        for key, value in (definitions or {}).items():
            arguments.append(f"-D{key}={value}")
        result = ConfigureResult()

        async def parse(line: str) -> None:
            if line.startswith("-- Configuring done"):
                result.configured = True
            if line.startswith("-- Generating done"):
                result.generated = True
            if on_progress is not None and line.startswith(("-- ", "CMake Error", "CMake Warning")):
                await on_progress(line.removeprefix("-- "))

        result.return_code, result.process_id = await execute(
            source,
            arguments,
            timeout,
            parse,
        )
        return result

    async def build(
        source: Path,
        build_directory: Path | None,
        *,
        preset: str | None = None,
        configuration: str | None = None,
        targets: Sequence[str] | None = None,
        parallel: int | None = None,
        timeout: ProcessTimeout = ProcessTimeout(total=600),
        on_progress: Progress | None = None,
    ) -> BuildResult:
        arguments = ["--build"]
        if preset is None:
            if build_directory is None:
                raise ToolCommandError("A plain build requires a build directory.")
            arguments.append(str(build_directory))
        else:
            arguments.extend(("--preset", preset))
        if configuration is not None:
            arguments.extend(("--config", configuration))
        if targets is not None:
            arguments.extend(("--target", *targets))
        if parallel is not None:
            arguments.extend(("--parallel", str(parallel)))
        result = BuildResult()

        async def parse(line: str) -> None:
            step = re.match(r"\[(\d+)/(\d+)\]\s+", line)
            if step:
                result.completed_steps = int(step.group(1))
                result.total_steps = int(step.group(2))
            if on_progress is not None and (
                step
                or re.match(r"\[\s*\d+%\]", line)
                or line.startswith(("FAILED:", "ninja:", "MSBuild version"))
            ):
                await on_progress(line)

        result.return_code, result.process_id = await execute(
            source,
            arguments,
            timeout,
            parse,
        )
        return result

    methods: Methods = {
        "version": version,
        "presets": presets,
        "configure": configure,
        "build": build,
    }
    return ToolSpec(INFO.name, INFO.kind, path, methods)


INFO = ToolInfo(name='cmake', kind=ToolKind.BUILD_SYSTEM, create_spec=create_spec)
