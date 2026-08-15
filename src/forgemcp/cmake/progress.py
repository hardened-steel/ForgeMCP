"""Safe CMake/CTest progress derivation from bounded local observations."""

from __future__ import annotations

import asyncio
import re
from time import monotonic

from forgemcp.plugins import ProgressUpdate, ToolExecutionContext
from forgemcp.processes import ProcessOutputEvent


HEARTBEAT_SECONDS = 2.0
"""Bounded quiet-operation activity interval; adapter rate limiting still applies."""
_MAX_PROGRESS_ITEMS = 1_000_000
_MAX_PROGRESS_LABEL = 96
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPACE = re.compile(r"\s+")
_SENSITIVE = re.compile(r"\b(?:api[ _-]?key|authorization|password|secret|token)\b", re.IGNORECASE)
_NINJA = re.compile(r"^\[(?P<completed>[1-9][0-9]*)/(?P<total>[1-9][0-9]*)\]\s+")
_CTEST_START = re.compile(r"^Start\s+(?P<index>[1-9][0-9]*):\s*(?P<name>.+)$")
_CTEST_COMPLETE = re.compile(
    r"^\s*(?P<completed>[1-9][0-9]*)/(?P<total>[1-9][0-9]*)\s+"
    r"Test\s+#(?P<index>[1-9][0-9]*):\s*(?P<name>.+?)\s+\.{3,}\s*"
    r"(?:Passed|Failed|\*\*\*[^\r\n]*)$"
)


def safe_progress_label(value: str) -> str | None:
    """Return a bounded display-only target/test name or omit it entirely."""
    normalized = _SPACE.sub(" ", value.strip())
    if (
        not normalized
        or len(normalized) > _MAX_PROGRESS_LABEL
        or _CONTROL.search(normalized)
        or "/" in normalized
        or "\\" in normalized
        or "=" in normalized
        or _SENSITIVE.search(normalized)
        or normalized.startswith("-")
    ):
        return None
    return normalized


class CMakeOutputProgressObserver:
    """Trusted local parser that emits only fixed, normalized progress labels."""

    def __init__(self, context: ToolExecutionContext, operation: str) -> None:
        self._context = context
        self._operation = operation
        self._buffers = {"stdout": "", "stderr": ""}
        self._last_ninja: tuple[int, int] | None = None
        self._last_ctest: tuple[int, int] | None = None

    async def __call__(self, event: ProcessOutputEvent) -> None:
        # Observation chunks deliberately retain no durable raw text.  A small
        # incomplete-line tail is enough for known CMake/Ninja/CTest formats.
        previous = self._buffers[event.stream]
        combined = (previous + event.text)[-8_192:]
        lines = combined.splitlines()
        self._buffers[event.stream] = "" if combined.endswith(("\n", "\r")) else (lines.pop() if lines else combined)
        for line in lines:
            await self._observe_line(line)

    async def _observe_line(self, line: str) -> None:
        if self._operation == "configure":
            normalized = line.strip().casefold()
            if normalized.endswith("configuring done"):
                await self._context.report_progress(ProgressUpdate(2, None, "Generating build files"))
            elif normalized.endswith("generating done"):
                await self._context.report_progress(ProgressUpdate(2, None, "Finalizing build files"))
            return
        if self._operation == "build":
            match = _NINJA.match(line)
            if match is None:
                return
            completed, total = int(match["completed"]), int(match["total"])
            if total > _MAX_PROGRESS_ITEMS or completed > total:
                return
            pair = (completed, total)
            if pair == self._last_ninja or (
                self._last_ninja is not None
                and total == self._last_ninja[1]
                and completed < self._last_ninja[0]
            ):
                return
            self._last_ninja = pair
            await self._context.report_progress(ProgressUpdate(completed, total, "Building"))
            return
        if self._operation != "test":
            return
        start = _CTEST_START.match(line)
        if start is not None:
            name = safe_progress_label(start["name"])
            if name is not None:
                await self._context.report_progress(ProgressUpdate(2, None, f"Running test: {name}"))
            return
        match = _CTEST_COMPLETE.match(line)
        if match is None:
            return
        completed, total = int(match["completed"]), int(match["total"])
        if total > _MAX_PROGRESS_ITEMS or completed > total:
            return
        pair = (completed, total)
        if pair == self._last_ctest or (
            self._last_ctest is not None
            and total == self._last_ctest[1]
            and completed < self._last_ctest[0]
        ):
            return
        self._last_ctest = pair
        name = safe_progress_label(match["name"])
        message = "Test completed" if name is None else f"Test completed: {name}"
        await self._context.report_progress(ProgressUpdate(completed, total, message))


async def run_heartbeat(
    context: ToolExecutionContext,
    *,
    operation: str,
    phase: float,
) -> None:
    """Emit bounded elapsed activity while a child produces no recognized output."""
    started = monotonic()
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        elapsed = max(1, int(monotonic() - started))
        await context.report_progress(
            ProgressUpdate(phase, None, f"{operation.capitalize()} running ({elapsed}s)")
        )
