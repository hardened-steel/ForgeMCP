"""Concurrent application-scoped registry for cached status providers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from forgemcp.project.errors import (
    DuplicateProjectStatusProviderError,
    ProjectStatusRegistryClosedError,
)
from forgemcp.project.models import MAX_COMPONENTS, ComponentStatus


DEFAULT_PROVIDER_TIMEOUT_SECONDS = 0.25
DEFAULT_TOTAL_TIMEOUT_SECONDS = 1.0


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


class ProjectStatusRegistry:
    """Own providers for one ForgeApplication and aggregate them concurrently."""

    def __init__(self) -> None:
        self._providers: dict[str, ProjectStatusProvider] = {}
        self._closed = False
        self._active_tasks: set[asyncio.Task[ComponentStatus]] = set()

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

        if self._closed:
            raise ProjectStatusRegistryClosedError("Project status is unavailable during shutdown.")
        if provider_timeout_seconds <= 0 or total_timeout_seconds <= 0:
            raise ValueError("Project status deadlines must be positive.")
        providers = tuple(sorted(self._providers.items()))
        tasks: dict[str, asyncio.Task[ComponentStatus]] = {}
        for provider_id, provider in providers:
            task = asyncio.create_task(
                self._snapshot_one(provider_id, provider, provider_timeout_seconds),
                name=f"forgemcp-project-status-{provider_id}",
            )
            tasks[provider_id] = task
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

        try:
            done, pending = await asyncio.wait(
                tuple(tasks.values()), timeout=total_timeout_seconds
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
        )

    async def aclose(self) -> None:
        """Close the registry and cancel/join every in-flight provider task."""

        if self._closed:
            return
        self._closed = True
        await self._cancel_and_join(tuple(self._active_tasks))

    @staticmethod
    async def _snapshot_one(
        provider_id: str,
        provider: ProjectStatusProvider,
        timeout_seconds: float,
    ) -> ComponentStatus:
        result = await asyncio.wait_for(provider.snapshot_status(), timeout=timeout_seconds)
        if not isinstance(result, ComponentStatus):
            # Validate only typed public models. Dict payloads are deliberately rejected.
            raise TypeError("Project status providers must return ComponentStatus.")
        try:
            result = ComponentStatus.model_validate(result.model_dump())
        except ValidationError as error:
            raise TypeError("Project status provider returned invalid bounded data.") from error
        if result.id != provider_id:
            raise ValueError("Project status provider result id does not match its registration.")
        return result

    @staticmethod
    async def _cancel_and_join(tasks: tuple[asyncio.Task[ComponentStatus], ...]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
