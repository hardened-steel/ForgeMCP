"""Typed methods for ctest."""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, Protocol, TypedDict
import xml.etree.ElementTree as ET

from pydantic import BaseModel, Field

from forgemcp.process.errors import ProcessError
from forgemcp.process.models import ProcessTimeout
from forgemcp.process.service import ProcessService

from ..errors import ToolCommandError, ToolParserError
from ..spec import ToolInfo, ToolKind, ToolSpec


type Progress = Callable[[str], Awaitable[None]]


class TestCase(BaseModel):
    """One JUnit test outcome with optional duration and failure or skip explanation."""

    name: str
    status: Literal["passed", "failed", "skipped", "not_run"]
    duration_seconds: float | None = None
    message: str | None = None


class TestResult(BaseModel):
    """A CTest run's exit code, process reference, and parsed test cases."""

    return_code: int
    process_id: int
    tests: list[TestCase] = Field(default_factory=list)


def parse_report(path: Path) -> list[TestCase]:
    """Read a JUnit report and classify each testcase as passed, failed, skipped, or not run."""
    if not path.is_file():
        raise ToolParserError("CTest did not produce a JUnit report.")
    try:
        data = path.read_text(encoding="utf-8")
        root = ET.fromstring(data)
        results = []
        for case in root.iter("testcase"):
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            status = "passed"
            message = None
            if failure is not None or error is not None:
                status = "failed"
                problem = failure if failure is not None else error
                message = problem.get("message") or problem.text or "Test failed."
            elif skipped is not None:
                status = "skipped"
                message = skipped.get("message") or skipped.text
            elif case.get("status") in ("notrun", "disabled"):
                status = "not_run"
            elapsed = case.get("time")
            results.append(
                TestCase(
                    name=case.get("name", ""),
                    status=status,
                    duration_seconds=float(elapsed) if elapsed is not None else None,
                    message=message,
                )
            )
        return results
    except (ET.ParseError, ValueError, OSError) as error:
        raise ToolParserError("Cannot parse the CTest JUnit report.") from error


class Test(Protocol):
    """The callable contract for running CTest with a managed JUnit report."""

    async def __call__(
        self,
        source: Path,
        build_directory: Path | None,
        *,
        report_path: Path,
        preset: str | None = None,
        configuration: str | None = None,
        names: Sequence[str] | None = None,
        parallel: int | None = None,
        timeout: ProcessTimeout = ProcessTimeout(total=600),
        on_progress: Progress | None = None,
    ) -> TestResult:
        """Run selected tests and return the process reference and parsed JUnit outcomes."""
        ...


class Methods(TypedDict):
    """The typed operations supported by the bound ctest executable."""

    version: Callable[[], Awaitable[str]]
    test: Test


def create_spec(
    path: Path,
    processes: ProcessService,
    environment: Mapping[str, str] | None = None,
    inherit_environment: bool = True,
) -> ToolSpec:
    """Bind the resolved ctest executable to process execution and environment settings."""
    path = path.resolve()

    async def version() -> str:
        """Run a bounded version probe, drain both output streams, and parse the ctest banner."""
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
                'ctest version ([0-9][0-9A-Za-z.+~-]*)',
                text,
                re.IGNORECASE | re.MULTILINE,
            ):
                return match.group(1)
        raise ToolParserError(f"Cannot parse {INFO.name} version.")

    async def test(
        source: Path,
        build_directory: Path | None,
        *,
        report_path: Path,
        preset: str | None = None,
        configuration: str | None = None,
        names: Sequence[str] | None = None,
        parallel: int | None = None,
        timeout: ProcessTimeout = ProcessTimeout(total=600),
        on_progress: Progress | None = None,
    ) -> TestResult:
        """Run CTest with exact-name filtering and parse its JUnit outcomes and progress."""
        if preset is None and build_directory is None:
            raise ToolCommandError("A plain test run requires a build directory.")
        arguments = (
            ["--test-dir", str(build_directory)]
            if preset is None
            else ["--preset", preset]
        )
        arguments.extend(("--output-junit", str(report_path), "--output-on-failure"))
        if preset is None:
            arguments.append("--no-tests=error")
        if configuration is not None:
            arguments.extend(("--build-config", configuration))
        if names is not None:
            pattern = "^(" + "|".join(re.escape(name) for name in names) + ")$"
            arguments.extend(("--tests-regex", pattern))
        if parallel is not None:
            arguments.extend(("--parallel", str(parallel)))
        buffers = {"stdout": "", "stderr": ""}

        async def parse_progress(line: str) -> None:
            """Forward recognized CTest execution and summary lines to the progress callback."""
            if on_progress is not None and re.search(
                r"Start\s+\d+:|\d+/\d+\s+Test|tests passed|Total Test time",
                line,
            ):
                await on_progress(line.strip())

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
                        await parse_progress(line)
                for line in buffers.values():
                    await parse_progress(line)
                code = await session.wait()
            cases = parse_report(report_path) if report_path.exists() or code == 0 else []
            return TestResult(
                return_code=code,
                process_id=session.process_id,
                tests=cases,
            )
        except (ProcessError, ToolParserError) as error:
            raise ToolCommandError(
                str(error),
                process_id=session.process_id if session is not None else None,
            ) from error

    methods: Methods = {"version": version, "test": test}
    return ToolSpec(INFO.name, INFO.kind, path, methods)


INFO = ToolInfo(name='ctest', kind=ToolKind.TEST_RUNNER, create_spec=create_spec)
