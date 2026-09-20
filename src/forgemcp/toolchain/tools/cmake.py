"""Typed methods for cmake."""

import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TypedDict

from forgemcp.process.errors import ProcessError
from forgemcp.process.models import ProcessTimeout
from forgemcp.process.service import ProcessService

from ..errors import ToolCommandError, ToolParserError
from ..spec import ToolInfo, ToolKind, ToolSpec


class Methods(TypedDict):
    version: Callable[[], Awaitable[str]]


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

    methods: Methods = {"version": version}
    return ToolSpec(INFO.name, INFO.kind, path, methods)


INFO = ToolInfo(name='cmake', kind=ToolKind.BUILD_SYSTEM, create_spec=create_spec)
