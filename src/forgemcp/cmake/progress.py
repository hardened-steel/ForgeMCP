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
_NINJA = re.compile(r"^\[(?P<completed>[1-9][0-9]*)/(?P<total>[1-9][0-9]*)\] ")
_CTEST_COMPLETE = re.compile(
    r"^\s*(?P<completed>[1-9][0-9]*)/(?P<total>[1-9][0-9]*)\s+"
    r"Test\s+#(?P<index>[1-9][0-9]*):\s*(?P<name>.+?)\s+\.{3,}\s*"
    r"(?:(?:Passed|Failed)(?:\s+[0-9]+(?:\.[0-9]+)?\s+sec)?|\*\*\*[^\r\n]*)$"
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
        self._discarding_line = {"stdout": False, "stderr": False}
        self._last_ninja: tuple[int, int] | None = None
        self._last_ctest: tuple[int, int] | None = None
        self._exact_disabled = False

    async def __call__(self, event: ProcessOutputEvent) -> None:
        # Observation chunks deliberately retain no durable raw text.  Keep
        # only one bounded incomplete line per independent stream, and never
        # treat the suffix of an oversized line as a new strict parser line.
        await self._consume_text(event.stream, event.text, truncated=event.truncated)

    async def aclose(self) -> None:
        """Flush an EOF-terminated final line after ProcessRuntime drains it."""
        for stream in ("stdout", "stderr"):
            line = self._buffers[stream]
            self._buffers[stream] = ""
            if line and not self._discarding_line[stream]:
                await self._observe_line(line)
            self._discarding_line[stream] = False

    async def _consume_text(self, stream: str, text: str, *, truncated: bool) -> None:
        if truncated:
            self._buffers[stream] = ""
            self._discarding_line[stream] = True
        combined = self._buffers[stream] + text
        self._buffers[stream] = ""
        while combined:
            match = re.search(r"[\r\n]", combined)
            if match is None:
                if self._discarding_line[stream]:
                    return
                if len(combined) > 8_192:
                    self._discarding_line[stream] = True
                    return
                self._buffers[stream] = combined
                return
            line = combined[:match.start()]
            next_index = match.end()
            if combined[match.start()] == "\r" and next_index < len(combined) and combined[next_index] == "\n":
                next_index += 1
            combined = combined[next_index:]
            if self._discarding_line[stream]:
                self._discarding_line[stream] = False
                continue
            if len(line) > 8_192:
                continue
            await self._observe_line(line)

    async def _observe_line(self, line: str) -> None:
        if self._operation == "configure":
            normalized = line.strip().casefold()
            if normalized.endswith("configuring done"):
                await self._context.report_progress(ProgressUpdate(0, None, "Generating build files"))
            elif normalized.endswith("generating done"):
                await self._context.report_progress(ProgressUpdate(0, None, "Finalizing build files"))
            return
        if self._operation == "build":
            match = _NINJA.match(line)
            if match is None:
                return
            completed, total = int(match["completed"]), int(match["total"])
            if total > _MAX_PROGRESS_ITEMS or completed > total:
                return
            if not self._accept_exact("ninja", completed, total):
                return
            # A build can print a final Ninja counter before CMake reports a
            # later failure.  Defer 100% to the known-success terminal update.
            if completed < total:
                await self._context.report_progress(ProgressUpdate(completed, total, "Building"))
            return
        if self._operation != "test":
            return
        match = _CTEST_COMPLETE.match(line)
        if match is None:
            return
        completed, total = int(match["completed"]), int(match["total"])
        if total > _MAX_PROGRESS_ITEMS or completed > total:
            return
        if not self._accept_exact("ctest", completed, total):
            return
        if completed == total:
            return
        # CTest's live output belongs to the project process, not to the
        # already model-validated request.  Do not publish its test-name
        # field, even when it superficially resembles a safe label.
        await self._context.report_progress(ProgressUpdate(completed, total, "Test completed"))

    def terminal_success_update(self, message: str) -> ProgressUpdate:
        """Return one terminal success without fabricating completion on failure."""
        exact = self._last_ninja if self._operation == "build" else self._last_ctest if self._operation == "test" else None
        if exact is not None and not self._exact_disabled:
            _, total = exact
            return ProgressUpdate(total, total, message, terminal=True, completed=True)
        return ProgressUpdate(0, None, message, terminal=True, completed=True)

    def _accept_exact(self, kind: str, completed: int, total: int) -> bool:
        if self._exact_disabled:
            return False
        previous = self._last_ninja if kind == "ninja" else self._last_ctest
        if previous is not None:
            previous_completed, previous_total = previous
            # A counter reset or changed total is a distinct internal build
            # phase, not a new MCP measurement.  Fall back to heartbeats.
            if total != previous_total or completed < previous_completed:
                self._exact_disabled = True
                return False
            if (completed, total) == previous:
                return False
        if kind == "ninja":
            self._last_ninja = (completed, total)
        else:
            self._last_ctest = (completed, total)
        return True


async def run_heartbeat(
    context: ToolExecutionContext,
    *,
    operation: str,
    phase: float = 0,
) -> None:
    """Emit bounded elapsed activity while a child produces no recognized output."""
    started = monotonic()
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        elapsed = max(1, int(monotonic() - started))
        await context.report_progress(
            ProgressUpdate(phase, None, f"{operation.capitalize()} running ({elapsed}s)")
        )
