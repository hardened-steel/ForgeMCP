"""Application-local handoff of validated compilation database metadata."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable

from forgemcp.cmake.models import CompilationDatabaseStatus


CompilationDatabaseHandler = Callable[[CompilationDatabaseStatus], object | Awaitable[object]]
COMPILATION_DATABASE_HANDLER_TIMEOUT_SECONDS = 15.0


class CompilationDatabaseSubscription:
    """Idempotent token for a bounded compilation-database integration callback."""

    __slots__ = ("_registry", "_name")

    def __init__(self, registry: "CompilationDatabaseRegistry", name: str) -> None:
        self._registry = registry
        self._name = name

    def close(self) -> None:
        self._registry.unsubscribe(self._name)


class CompilationDatabaseRegistry:
    """Small application-scoped registry for the latest validated database.

    Configure calls await at most a fixed number of consumers, each under a
    bounded timeout.  Consumer failure is converted to an integration warning;
    a successful CMake configure remains successful.
    """

    __slots__ = ("_logger", "_latest", "_handlers", "_degraded", "_retired_tasks")

    def __init__(self, logger: object) -> None:
        self._logger = logger
        self._latest: CompilationDatabaseStatus | None = None
        self._handlers: dict[str, CompilationDatabaseHandler] = {}
        self._degraded = False
        self._retired_tasks: set[asyncio.Future[object]] = set()

    @property
    def latest(self) -> CompilationDatabaseStatus | None:
        return self._latest

    @property
    def degraded(self) -> bool:
        return self._degraded

    def subscribe(self, name: str, handler: CompilationDatabaseHandler) -> CompilationDatabaseSubscription:
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 64
            or not callable(handler)
            or name in self._handlers
            or len(self._handlers) + len(self._retired_tasks) >= 8
        ):
            raise ValueError("Compilation database subscription is unavailable.")
        self._handlers[name] = handler
        return CompilationDatabaseSubscription(self, name)

    def unsubscribe(self, name: str) -> None:
        self._handlers.pop(name, None)

    async def publish(self, status: CompilationDatabaseStatus) -> None:
        self._latest = status
        for name, handler in tuple(self._handlers.items()):
            task: asyncio.Future[object] | None = None
            try:
                result = handler(status)
                if inspect.isawaitable(result):
                    task = asyncio.ensure_future(result)
                    completed, _ = await asyncio.wait(
                        {task}, timeout=COMPILATION_DATABASE_HANDLER_TIMEOUT_SECONDS
                    )
                    if not completed:
                        self._handlers.pop(name, None)
                        task.cancel()
                        self._retire_task(task)
                        raise TimeoutError
                    await task
            except asyncio.CancelledError:
                if task is not None and not task.done():
                    task.cancel()
                    self._retire_task(task)
                raise
            except Exception:
                self._degraded = True
                # No paths, compiler arguments, source, or exception payload.
                warning = getattr(self._logger, "warning", None)
                if callable(warning):
                    warning("compilation_database_subscriber_degraded", subscriber=name)

    def _retire_task(self, task: asyncio.Future[object]) -> None:
        """Bound cancellation-suppressing callback work after detaching it."""
        self._retired_tasks.add(task)

        def discard(completed: asyncio.Future[object]) -> None:
            self._retired_tasks.discard(completed)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                completed.result()

        task.add_done_callback(discard)
