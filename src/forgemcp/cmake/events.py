"""Application-local handoff of validated compilation database metadata."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable

from forgemcp.cmake.models import CompilationDatabaseStatus


CompilationDatabaseHandler = Callable[[CompilationDatabaseStatus], object | Awaitable[object]]


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

    __slots__ = ("_logger", "_latest", "_handlers", "_degraded")

    def __init__(self, logger: object) -> None:
        self._logger = logger
        self._latest: CompilationDatabaseStatus | None = None
        self._handlers: dict[str, CompilationDatabaseHandler] = {}
        self._degraded = False

    @property
    def latest(self) -> CompilationDatabaseStatus | None:
        return self._latest

    @property
    def degraded(self) -> bool:
        return self._degraded

    def subscribe(self, name: str, handler: CompilationDatabaseHandler) -> CompilationDatabaseSubscription:
        if not isinstance(name, str) or not name or len(name) > 64 or not callable(handler) or name in self._handlers or len(self._handlers) >= 8:
            raise ValueError("Compilation database subscription is unavailable.")
        self._handlers[name] = handler
        return CompilationDatabaseSubscription(self, name)

    def unsubscribe(self, name: str) -> None:
        self._handlers.pop(name, None)

    async def publish(self, status: CompilationDatabaseStatus) -> None:
        self._latest = status
        for name, handler in tuple(self._handlers.items()):
            try:
                result = handler(status)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=15.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._degraded = True
                # No paths, compiler arguments, source, or exception payload.
                warning = getattr(self._logger, "warning", None)
                if callable(warning):
                    warning("compilation_database_subscriber_degraded", subscriber=name)
