"""Independent progress throttling and metadata preservation tests."""

import asyncio
from types import SimpleNamespace

import forgemcp.progress as module


def test_reporters_throttle_independently_and_preserve_metadata(monkeypatch):
    """Verify separate invocation throttles preserve the metadata of emitted updates."""
    now = 0.0
    events = []
    monkeypatch.setattr(module, "monotonic", lambda: now)

    async def collect(value, total=None, message=None):
        """Record the progress value, total, and message emitted by the reporter."""
        events.append((value, total, message))

    async def scenario():
        """Exercise two reporters against a deterministic clock and compare emitted updates."""
        nonlocal now
        ctx = SimpleNamespace(report_progress=collect)
        first = module.progress(ctx, interval=1)
        second = module.progress(ctx, interval=1)
        await first(0, total=10, message="start")
        now = 0.5
        await first(4, total=10, message="skipped")
        await second(0, message="independent")
        now = 1.0
        await first(8, total=10, message="latest")
        await first(10, total=10, message="no completion bypass")
        now = 3.0
        await asyncio.sleep(0)
        assert events == [(0, 10, "start"), (0, None, "independent"), (8, 10, "latest")]
        unthrottled = module.progress(ctx, interval=0)
        await unthrottled(0)
        await unthrottled(1)
        assert events[-2:] == [(0, None, None), (1, None, None)]

    asyncio.run(scenario())
