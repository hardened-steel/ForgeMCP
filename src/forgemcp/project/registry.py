"""Concurrent application-scoped registry for cached status providers."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import timedelta
from math import isfinite
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from forgemcp.project.errors import (
    DuplicateProjectStatusProviderError,
    ProjectStatusRegistryClosedError,
)
from forgemcp.project.models import MAX_COMPONENTS, ComponentStatus, utc_now


DEFAULT_PROVIDER_TIMEOUT_SECONDS = 0.25
DEFAULT_TOTAL_TIMEOUT_SECONDS = 1.0
DEFAULT_CLEANUP_TIMEOUT_SECONDS = 0.05
MAX_OBSERVATION_AGE = timedelta(days=1)
STALE_OBSERVATION_AGE = timedelta(minutes=5)
MAX_FUTURE_CLOCK_SKEW = timedelta(seconds=5)


@runtime_checkable
class ProjectStatusProvider(Protocol):
    """Transport-neutral provider of one safe cached component snapshot."""

    @property
    def id(self) -> str:
        """Return the unique stable provider identifier."""

    async def snapshot_status(self) -> ComponentStatus:
        """Return cached state only, without probes, file reads, or lifecycle changes."""


@dataclass(frozen=True, slots=True)
class ProjectStatusSnapshot:
    """Registry result with safe failure categories, never exception text."""

    components: tuple[ComponentStatus, ...]
    failed_components: tuple[str, ...]
    timed_out_components: tuple[str, ...]
    provider_ids: tuple[str, ...]


class ProjectStatusRegistry:
    """Own providers for one ForgeApplication and aggregate them concurrently."""

    def __init__(self) -> None:
        self._providers: dict[str, ProjectStatusProvider] = {}
        self._closed = False
        self._active_tasks: set[asyncio.Task[ComponentStatus]] = set()
        self._snapshot_task: asyncio.Task[ProjectStatusSnapshot] | None = None
        self._singleflight_lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    def provider_ids(self) -> tuple[str, ...]:
        """Return provider identifiers in deterministic lexical order."""

        return tuple(sorted(self._providers))

    def register(self, provider: ProjectStatusProvider) -> None:
        """Register one provider, rejecting malformed and duplicate identifiers."""

        if self._closed:
            raise ProjectStatusRegistryClosedError("Project status is unavailable during shutdown.")
        if not isinstance(provider, ProjectStatusProvider):
            raise TypeError("Project status providers must implement id and snapshot_status().")
        provider_id = provider.id
        # Reuse the public model's strict identifier validation at the trust boundary.
        try:
            ComponentStatus.model_fields["id"].annotation  # keep schema as the single declaration
            if (
                not isinstance(provider_id, str)
                or not provider_id
                or len(provider_id) > 64
                or not provider_id[0].islower()
                or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for character in provider_id)
            ):
                raise ValueError
        except (TypeError, ValueError) as error:
            raise ValueError("Project status provider id is invalid.") from error
        if provider_id in self._providers:
            raise DuplicateProjectStatusProviderError(
                "A project status provider with this id is already registered."
            )
        if len(self._providers) >= MAX_COMPONENTS:
            raise ValueError("Project status provider capacity has been reached.")
        self._providers[provider_id] = provider

    def unregister(self, provider_id: str) -> None:
        """Idempotently prevent a provider from participating in future snapshots."""

        self._providers.pop(provider_id, None)

    async def snapshot_all(
        self,
        *,
        provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        total_timeout_seconds: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
    ) -> ProjectStatusSnapshot:
        """Snapshot all current providers with bounded individual and total deadlines."""

        if (
            isinstance(provider_timeout_seconds, bool)
            or isinstance(total_timeout_seconds, bool)
            or not isinstance(provider_timeout_seconds, (int, float))
            or not isinstance(total_timeout_seconds, (int, float))
            or not isfinite(provider_timeout_seconds)
            or not isfinite(total_timeout_seconds)
            or provider_timeout_seconds <= 0
            or total_timeout_seconds <= 0
        ):
            raise ValueError("Project status deadlines must be positive.")
        async with self._singleflight_lock:
            if self._closed:
                raise ProjectStatusRegistryClosedError("Project status is unavailable during shutdown.")
            if self._snapshot_task is None or self._snapshot_task.done():
                providers = tuple(sorted(self._providers.items()))
                self._snapshot_task = asyncio.create_task(
                    self._snapshot_all_once(
                        providers=providers,
                        provider_timeout_seconds=provider_timeout_seconds,
                        total_timeout_seconds=total_timeout_seconds,
                    ),
                    name="forgemcp-project-status-snapshot",
                )
                self._snapshot_task.add_done_callback(self._consume_task_result)
            snapshot_task = self._snapshot_task

        # One caller cancelling must not cancel the shared snapshot used by peers.
        result = await asyncio.shield(snapshot_task)
        async with self._singleflight_lock:
            if self._snapshot_task is snapshot_task:
                self._snapshot_task = None
        return result

    async def _snapshot_all_once(
        self,
        *,
        providers: tuple[tuple[str, ProjectStatusProvider], ...],
        provider_timeout_seconds: float,
        total_timeout_seconds: float,
    ) -> ProjectStatusSnapshot:
        """Run one shared collection; overlapping callers await this immutable result."""

        tasks: dict[str, asyncio.Task[ComponentStatus]] = {}
        for provider_id, provider in providers:
            task = asyncio.create_task(
                self._snapshot_one(provider_id, provider, provider_timeout_seconds),
                name=f"forgemcp-project-status-{provider_id}",
            )
            tasks[provider_id] = task
            self._active_tasks.add(task)
            task.add_done_callback(self._provider_task_done)

        try:
            done, pending = await asyncio.wait(
                tuple(tasks.values()),
                timeout=min(provider_timeout_seconds, total_timeout_seconds),
            ) if tasks else (set(), set())
        except asyncio.CancelledError:
            await self._cancel_and_join(tuple(tasks.values()))
            raise

        globally_timed_out = {
            provider_id for provider_id, task in tasks.items() if task in pending
        }
        await self._cancel_and_join(tuple(pending))
        components: list[ComponentStatus] = []
        failed: list[str] = []
        timed_out: list[str] = sorted(globally_timed_out)
        for provider_id, task in tasks.items():
            if provider_id in globally_timed_out:
                continue
            try:
                component = task.result()
            except TimeoutError:
                timed_out.append(provider_id)
            except (asyncio.CancelledError, Exception):
                failed.append(provider_id)
            else:
                components.append(component)
        return ProjectStatusSnapshot(
            components=tuple(sorted(components, key=lambda item: item.id)),
            failed_components=tuple(sorted(failed)),
            timed_out_components=tuple(sorted(set(timed_out))),
            provider_ids=tuple(provider_id for provider_id, _ in providers),
        )

    async def aclose(self) -> None:
        """Close the registry and cancel/join every in-flight provider task."""

        if self._closed:
            return
        self._closed = True
        tasks: set[asyncio.Task[object]] = set(self._active_tasks)
        if self._snapshot_task is not None and not self._snapshot_task.done():
            tasks.add(self._snapshot_task)
        await self._cancel_and_join(tuple(tasks))

    @staticmethod
    async def _snapshot_one(
        provider_id: str,
        provider: ProjectStatusProvider,
        timeout_seconds: float,
    ) -> ComponentStatus:
        del timeout_seconds  # the shared collector enforces the common provider deadline
        result = await provider.snapshot_status()
        if not isinstance(result, ComponentStatus):
            # Validate only typed public models. Dict payloads are deliberately rejected.
            raise TypeError("Project status providers must return ComponentStatus.")
        try:
            result = ComponentStatus.model_validate(result.model_dump(warnings=False))
        except ValidationError as error:
            raise TypeError("Project status provider returned invalid bounded data.") from error
        if result.id != provider_id:
            raise ValueError("Project status provider result id does not match its registration.")
        now = utc_now()
        age = now - result.observed_at
        if age < -MAX_FUTURE_CLOCK_SKEW:
            raise ValueError("Project status provider observation is too far in the future.")
        if age > MAX_OBSERVATION_AGE:
            raise ValueError("Project status provider observation is too old.")
        if age > STALE_OBSERVATION_AGE and not result.stale:
            payload = result.model_dump(warnings=False)
            payload["stale"] = True
            result = ComponentStatus.model_validate(payload)
        return result

    @staticmethod
    async def _cancel_and_join(tasks: tuple[asyncio.Task[object], ...]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            # A trusted in-process provider may suppress cancellation. Never let
            # cleanup extend the public deadline without a bound; unfinished
            # tasks remain tracked until their done callback consumes the result.
            await asyncio.wait(tasks, timeout=DEFAULT_CLEANUP_TIMEOUT_SECONDS)

    def _provider_task_done(self, task: asyncio.Task[ComponentStatus]) -> None:
        self._active_tasks.discard(task)
        self._consume_task_result(task)

    @staticmethod
    def _consume_task_result(task: asyncio.Task[object]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()
