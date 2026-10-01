"""Per-invocation throttling of MCP progress notifications."""

from collections.abc import Awaitable, Callable
from math import isfinite
from time import monotonic

from mcp.server.mcpserver import Context


type Progress = Callable[[str], Awaitable[None]]


def validate_progress_interval(interval: float) -> float:
    """Validate an operator setting before accepting requests."""
    if not isfinite(interval) or interval < 0:
        raise ValueError("Progress interval must be finite and nonnegative.")
    return interval


def progress(
    ctx: Context,
    *,
    interval: float = 1.0,
) -> Callable[..., Awaitable[None]]:
    """Create a reporter for one invocation; skipped updates are never queued."""
    last_sent: float | None = None

    async def report(
        value: float,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        nonlocal last_sent
        now = monotonic()
        if last_sent is not None and now - last_sent < interval:
            return
        last_sent = now
        await ctx.report_progress(value, total=total, message=message)

    return report
