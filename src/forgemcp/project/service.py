"""Health and activity aggregation for Project Intelligence Phase 1."""

from __future__ import annotations

from pathlib import Path

from forgemcp.project.models import (
    MAX_CAPABILITIES,
    MAX_WARNINGS,
    ComponentState,
    ProjectActivity,
    ProjectHealth,
    ProjectStatus,
    utc_now,
)
from forgemcp.project.registry import ProjectStatusRegistry


_FUNDAMENTAL_COMPONENTS = frozenset({"core", "workspace", "process_runtime", "plugin_manager"})


class ProjectStatusService:
    """Aggregate registered providers without importing concrete feature services."""

    def __init__(
        self,
        registry: ProjectStatusRegistry,
        workspace_root: Path,
        *,
        provider_timeout_seconds: float = 0.25,
        total_timeout_seconds: float = 1.0,
    ) -> None:
        self._registry = registry
        self._workspace_root = str(workspace_root)
        self._provider_timeout_seconds = provider_timeout_seconds
        self._total_timeout_seconds = total_timeout_seconds

    async def status(self) -> ProjectStatus:
        """Return a bounded non-transactional snapshot from cached provider state."""

        snapshot = await self._registry.snapshot_all(
            provider_timeout_seconds=self._provider_timeout_seconds,
            total_timeout_seconds=self._total_timeout_seconds,
        )
        components = snapshot.components
        failed_ids = set(snapshot.failed_components)
        timed_out_ids = set(snapshot.timed_out_components)
        partial = bool(failed_ids or timed_out_ids)
        warnings = tuple((
            [f"component_status_failed:{provider_id}" for provider_id in sorted(failed_ids)]
            + [f"component_status_timed_out:{provider_id}" for provider_id in sorted(timed_out_ids)]
        )[:MAX_WARNINGS])
        health = self._health(components, partial=partial)
        activity = self._activity(components)
        capabilities = tuple(sorted(
            {item for component in components for item in component.capabilities}
        )[:MAX_CAPABILITIES])
        return ProjectStatus(
            generated_at=utc_now(),
            workspace_root=self._workspace_root,
            health=health,
            activity=activity,
            components=components,
            capabilities=capabilities,
            warnings=warnings,
            partial=partial,
            timed_out_components=tuple(sorted(timed_out_ids)),
        )

    @staticmethod
    def _health(components: tuple, *, partial: bool) -> ProjectHealth:
        by_id = {component.id: component for component in components}
        if any(
            by_id.get(component_id) is not None
            and by_id[component_id].state is ComponentState.FAILED
            for component_id in _FUNDAMENTAL_COMPONENTS
        ):
            return ProjectHealth.FAILED
        if partial or any(
            component.state in {ComponentState.DEGRADED, ComponentState.FAILED}
            for component in components
        ):
            return ProjectHealth.DEGRADED
        return ProjectHealth.HEALTHY

    @staticmethod
    def _activity(components: tuple) -> ProjectActivity:
        by_id = {component.id: component for component in components}
        debugger = by_id.get("debugger")
        if debugger is not None and debugger.state is ComponentState.PAUSED:
            return ProjectActivity.PAUSED
        if any(
            (component.id in {"cmake", "quality"} and component.state is ComponentState.ACTIVE)
            or (component.id == "debugger" and component.state in {ComponentState.STARTING, ComponentState.ACTIVE})
            or (component.id == "clangd" and component.state is ComponentState.STARTING)
            for component in components
        ):
            return ProjectActivity.BUSY
        return ProjectActivity.IDLE
