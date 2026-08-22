"""Bounded, application-local notifications for committed Workspace changes.

The event stream intentionally contains metadata only.  It is a coordination
mechanism for feature services (for example CMake and clangd), never a file
watcher and never a transport surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from forgemcp.models import FileChangeKind, FileSnapshot


MAX_MUTATION_SUBSCRIBERS = 16
MAX_PENDING_MUTATION_BATCHES = 32
MAX_MUTATION_HISTORY = 64
SUBSCRIBER_HANDLER_TIMEOUT_SECONDS = 15.0
SUBSCRIBER_CLOSE_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class WorkspaceMutation:
    """One content-free file effect in a committed Workspace transaction."""

    generation: int
    path: str
    kind: FileChangeKind
    before: FileSnapshot | None
    after: FileSnapshot | None
    operation_id: str


@dataclass(frozen=True, slots=True)
class WorkspaceMutationBatch:
    """The ordered notification emitted once after an atomic commit."""

    generation: int
    operation_id: str
    changes: tuple[WorkspaceMutation, ...]


WorkspaceMutationHandler = Callable[[WorkspaceMutationBatch], object | Awaitable[object]]


class _Logger(Protocol):
    def warning(self, event: str, **context: object) -> None:
        """Write a content-free integration warning."""


@dataclass(slots=True)
class _Subscriber:
    name: str
    handler: WorkspaceMutationHandler
    queue: asyncio.Queue[WorkspaceMutationBatch | None]
    worker: asyncio.Task[None] | None = None
    failures: int = 0
    dropped: int = 0


class WorkspaceMutationSubscription:
    """Idempotent ownership token for one application-local subscription."""

    __slots__ = ("_bus", "_name")

    def __init__(self, bus: "WorkspaceMutationBus", name: str) -> None:
        self._bus = bus
        self._name = name

    async def aclose(self) -> None:
        """Detach the subscriber and await its one bounded worker."""
        await self._bus.unsubscribe(self._name)


class WorkspaceMutationBus:
    """A bounded lifecycle-owned fan-out stream for one ForgeApplication.

    A successful filesystem commit has already happened before ``publish`` is
    called.  Individual subscriber failure and bounded queue saturation are
    deliberately recorded as degraded integration state and cannot alter that
    commit.  One subscriber owns at most one worker task and one fixed queue.
    """

    __slots__ = (
        "_logger", "_subscribers", "_generation", "_started", "_closed", "_degraded",
        "_publication_lock", "_history", "_retired_tasks",
    )

    def __init__(self, logger: _Logger) -> None:
        self._logger = logger
        self._subscribers: dict[str, _Subscriber] = {}
        self._generation = 0
        self._started = False
        self._closed = False
        self._degraded = False
        self._publication_lock = threading.Lock()
        self._history: deque[WorkspaceMutationBatch] = deque(maxlen=MAX_MUTATION_HISTORY)
        self._retired_tasks: set[asyncio.Task[object]] = set()

    @property
    def generation(self) -> int:
        """Return the latest emitted generation without exposing event data."""
        return self._generation

    @property
    def degraded(self) -> bool:
        """Whether an integration handler failed or its bounded queue overflowed."""
        return self._degraded

    def batches_since(self, generation: int) -> tuple[WorkspaceMutationBatch, ...] | None:
        """Return bounded metadata batches after ``generation`` when retained.

        ``None`` means the requested boundary fell out of bounded history.  A
        consumer must conservatively treat that as degraded/unknown rather than
        claiming a configure incorporated every intervening source mutation.
        """
        if not isinstance(generation, int) or generation < 0:
            raise ValueError("Workspace mutation generation is invalid.")
        with self._publication_lock:
            if self._history and generation < self._history[0].generation - 1:
                return None
            return tuple(batch for batch in self._history if batch.generation > generation)

    async def start(self) -> None:
        """Bind bounded worker tasks to the current application event loop."""
        if self._closed:
            return
        if self._started:
            return
        self._started = True
        for subscriber in self._subscribers.values():
            self._start_worker(subscriber)

    def subscribe(self, name: str, handler: WorkspaceMutationHandler) -> WorkspaceMutationSubscription:
        """Subscribe one named integration before or during application runtime."""
        if (
            self._closed
            or not isinstance(name, str)
            or not name
            or len(name) > 64
            or not callable(handler)
            or name in self._subscribers
            or len(self._subscribers) + len(self._retired_tasks) >= MAX_MUTATION_SUBSCRIBERS
        ):
            raise ValueError("Workspace mutation subscription is unavailable.")
        subscriber = _Subscriber(name=name, handler=handler, queue=asyncio.Queue(MAX_PENDING_MUTATION_BATCHES))
        self._subscribers[name] = subscriber
        if self._started:
            self._start_worker(subscriber)
        return WorkspaceMutationSubscription(self, name)

    async def unsubscribe(self, name: str) -> None:
        """Stop exactly one subscriber worker without affecting other integrations."""
        subscriber = self._subscribers.pop(name, None)
        if subscriber is None:
            return
        if subscriber.worker is not None:
            with contextlib.suppress(asyncio.QueueFull):
                subscriber.queue.put_nowait(None)
            subscriber.worker.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(subscriber.worker), timeout=SUBSCRIBER_CLOSE_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                # This is the expected result of cancelling a cooperative
                # queue worker during unregistration.
                return
            except (Exception, TimeoutError):
                self._degraded = True
                self._logger.warning("workspace_mutation_subscriber_degraded", subscriber=subscriber.name, reason="shutdown_timeout")
                self._retire_task(subscriber.worker)

    def publish(self, changes: tuple[tuple[str, FileChangeKind, FileSnapshot | None, FileSnapshot | None], ...], *, operation_id: str) -> None:
        """Queue one ordered post-commit batch without awaiting a subscriber.

        This method is intentionally synchronous because Workspace commits are
        synchronous.  It neither invokes handlers nor holds filesystem state.
        """
        with self._publication_lock:
            if self._closed or not changes:
                return
            self._generation += 1
            generation = self._generation
            batch = WorkspaceMutationBatch(
                generation=generation,
                operation_id=operation_id,
                changes=tuple(
                    WorkspaceMutation(
                        generation=generation,
                        path=path,
                        kind=kind,
                        before=before,
                        after=after,
                        operation_id=operation_id,
                    )
                    for path, kind, before, after in changes
                ),
            )
            self._history.append(batch)
            for subscriber in tuple(self._subscribers.values()):
                try:
                    subscriber.queue.put_nowait(batch)
                except asyncio.QueueFull:
                    subscriber.dropped += 1
                    self._degraded = True
                    self._logger.warning("workspace_mutation_subscriber_degraded", subscriber=subscriber.name, reason="queue_full")

    async def aclose(self) -> None:
        """Drain ownership deterministically and prevent further publication."""
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(*(self.unsubscribe(name) for name in tuple(self._subscribers)))

    def _start_worker(self, subscriber: _Subscriber) -> None:
        if subscriber.worker is None or subscriber.worker.done():
            subscriber.worker = asyncio.create_task(
                self._run_subscriber(subscriber), name=f"forgemcp-workspace-events-{subscriber.name}"
            )

    async def _run_subscriber(self, subscriber: _Subscriber) -> None:
        while True:
            batch = await subscriber.queue.get()
            if batch is None:
                return
            task: asyncio.Task[None] | None = None
            try:
                result = subscriber.handler(batch)
                if not inspect.isawaitable(result):
                    continue
                task = asyncio.create_task(result)
                done, _ = await asyncio.wait({task}, timeout=SUBSCRIBER_HANDLER_TIMEOUT_SECONDS)
                if not done:
                    self._degraded = True
                    subscriber.failures += 1
                    self._logger.warning("workspace_mutation_subscriber_degraded", subscriber=subscriber.name, reason="handler_timeout")
                    task.cancel()
                    self._retire_task(task)
                    return
                await task
            except asyncio.CancelledError:
                if task is not None and not task.done():
                    task.cancel()
                    self._retire_task(task)
                raise
            except Exception:
                subscriber.failures += 1
                self._degraded = True
                # Deliberately only plugin identity/category: no source path,
                # content, patch, exception value, or host information.
                self._logger.warning("workspace_mutation_subscriber_degraded", subscriber=subscriber.name, reason="handler_failure")

    def _retire_task(self, task: asyncio.Task[object]) -> None:
        """Keep cancellation-suppressing work bounded and visible to capacity checks."""
        self._retired_tasks.add(task)

        def discard(completed: asyncio.Task[object]) -> None:
            self._retired_tasks.discard(completed)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                completed.result()

        task.add_done_callback(discard)
