"""Typed methods for git."""

import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TypedDict

from forgemcp.process.errors import ProcessError, ProcessStartError
from forgemcp.process.models import ProcessEncoding, ProcessTimeout
from forgemcp.process.service import ProcessService

from ..errors import ToolCommandError, ToolParserError
from ..spec import ToolInfo, ToolKind, ToolSpec


@dataclass(frozen=True)
class CommandResult:
    """One completed Git command with bounded byte output and its process identity."""

    process_id: int
    return_code: int
    stdout: bytes
    stderr: str


class Run(Protocol):  # pylint: disable=too-few-public-methods
    """Execute an internally constructed Git argument list in a managed directory."""

    async def __call__(  # pylint: disable=too-many-arguments
        self,
        cwd: Path,
        arguments: Sequence[str],
        *,
        timeout: ProcessTimeout = ProcessTimeout(total=120),
        stdin: str | None = None,
        max_bytes: int = 4 * 1024 * 1024,
        read_only: bool = False,
    ) -> CommandResult:
        """Return literal Git output without interpreting the command's exit status."""


class Methods(TypedDict):
    """The typed operations supported by the bound git executable."""

    version: Callable[[], Awaitable[str]]
    run: Run


def command_environment(
    environment: Mapping[str, str] | None,
    inherit_environment: bool,
    read_only: bool,
) -> dict[str, str]:
    """Keep operator credentials while removing inherited repository and config redirection."""
    selected = dict(os.environ) if inherit_environment else {}
    selected.update(environment or {})
    redirected = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
    selected = {
        key: value
        for key, value in selected.items()
        if key.upper() not in redirected
        and not key.upper().startswith("GIT_CONFIG")
        and not key.upper().startswith("GIT_TRACE")
    }
    selected.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1" if read_only else "0",
            "LC_ALL": "C",
        }
    )
    # Clear inherited pathspec flags before the explicit literal-pathspecs option.
    for key in tuple(selected):
        if key.upper() in {
            "GIT_GLOB_PATHSPECS",
            "GIT_NOGLOB_PATHSPECS",
            "GIT_ICASE_PATHSPECS",
            "GIT_LITERAL_PATHSPECS",
            "GIT_OPTIONAL_LOCKS",
        }:
            del selected[key]
    if read_only:
        selected["GIT_OPTIONAL_LOCKS"] = "0"
    return selected


def create_spec(
    path: Path,
    processes: ProcessService,
    environment: Mapping[str, str] | None = None,
    inherit_environment: bool = True,
) -> ToolSpec:
    """Bind the resolved git executable to process execution and environment settings."""
    path = path.resolve()

    async def run(  # pylint: disable=too-many-arguments
        cwd: Path,
        arguments: Sequence[str],
        *,
        timeout: ProcessTimeout = ProcessTimeout(total=120),
        stdin: str | None = None,
        max_bytes: int = 4 * 1024 * 1024,
        read_only: bool = False,
    ) -> CommandResult:
        """Consume lossless framed Git output and leave full transcripts in ProcessService."""
        if max_bytes < 1:
            raise ToolCommandError(
                f"max_bytes={max_bytes} is invalid; the Git output byte limit must be positive."
            )
        if not arguments:
            raise ToolCommandError(
                "arguments is empty; Git requires at least one command argument."
            )
        for argument in arguments:
            if "\0" in argument:
                raise ToolCommandError(
                    f"arguments[{arguments.index(argument)}] contains NUL; "
                    "literal Git arguments must not contain NUL."
                )
        output = bytearray()
        errors = bytearray()
        process_id = None
        try:
            async with await processes.launch(
                path,
                [
                    "--no-pager",
                    "--literal-pathspecs",
                    "-c",
                    "color.ui=false",
                    "-c",
                    "core.quotepath=false",
                    *arguments,
                ],
                cwd=cwd,
                env=command_environment(environment, inherit_environment, read_only),
                inherit_environment=False,
                encoding=ProcessEncoding("latin_1", errors="strict"),
                timeout=timeout,
            ) as session:
                process_id = session.process_id
                if stdin is not None:
                    await session.write_stdin(stdin.encode("utf-8").decode("latin_1"))
                await session.close_stdin()
                async for chunk in session.output():
                    raw = chunk.text.encode("latin_1")
                    if chunk.stream == "stdout":
                        if len(output) + len(raw) > max_bytes:
                            raise ToolCommandError(
                                f"Git stdout exceeded its byte limit of {max_bytes} bytes; "
                                "the structured result was not returned partially. "
                                f"Inspect process_get(process_id={process_id}) for the transcript. "
                                "Narrow paths or line/commit ranges "
                                "when the calling tool supports it.",
                                process_id=process_id,
                            )
                        output.extend(raw)
                    else:
                        errors.extend(raw[:max(0, 16 * 1024 - len(errors))])
                code = await session.wait()
        except ProcessStartError as error:
            raise ToolCommandError(
                "The configured Git executable could not be started; ask the operator to check "
                "its installation and access permissions using toolsets_list() and "
                "toolset_get(toolset_id=...).",
                process_id=process_id,
            ) from error
        except ProcessError as error:
            raise ToolCommandError(
                f"Git command could not complete ({type(error).__name__}); "
                f"configured time limits: total={timeout.total} s, idle={timeout.idle} s. "
                "Inspect its process transcript with process_get(process_id=...).",
                process_id=process_id,
            ) from error
        return CommandResult(
            process_id=process_id,
            return_code=code,
            stdout=bytes(output),
            stderr=errors.decode("utf-8", errors="replace"),
        )

    async def version() -> str:
        """Run a bounded version probe, drain both output streams, and parse the git banner."""
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
                'git version ([0-9][^\\s]*)',
                text,
                re.IGNORECASE | re.MULTILINE,
            ):
                return match.group(1)
        raise ToolParserError(f"Cannot parse {INFO.name} version.")

    methods: Methods = {"version": version, "run": run}
    return ToolSpec(INFO.name, INFO.kind, path, methods)


INFO = ToolInfo(name='git', kind=ToolKind.VERSION_CONTROL, create_spec=create_spec)
