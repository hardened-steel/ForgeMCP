"""Health and activity aggregation for Project Intelligence Phase 1."""

from __future__ import annotations

from pathlib import Path

from forgemcp.project.models import (
    MAX_CAPABILITIES,
    MAX_STATUS_JSON_BYTES,
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
        # The root remains an application-internal composition dependency. A
        # project-status response must be portable and must not reveal a host
        # path, even when a provider fails.
        self._workspace_root = "configured"
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
        missing_fundamental_ids = _FUNDAMENTAL_COMPONENTS.difference(snapshot.provider_ids)
        partial = bool(failed_ids or timed_out_ids or missing_fundamental_ids)
        aggregate_warnings = (
            [f"component_status_failed:{provider_id}" for provider_id in sorted(failed_ids)]
            + [f"component_status_timed_out:{provider_id}" for provider_id in sorted(timed_out_ids)]
            + [f"critical_component_missing:{provider_id}" for provider_id in sorted(missing_fundamental_ids)]
            + sorted({warning for component in components for warning in component.warnings})
        )
        capabilities_all = sorted({item for component in components for item in component.capabilities})
        aggregate_truncated = (
            len(aggregate_warnings) > MAX_WARNINGS
            or len(capabilities_all) > MAX_CAPABILITIES
            or any("capabilities_truncated" in component.warnings for component in components)
        )
        if aggregate_truncated:
            aggregate_warnings.insert(0, "aggregate_fields_truncated")
        warnings = tuple(dict.fromkeys(aggregate_warnings))[:MAX_WARNINGS]
        health = self._health(
            components,
            partial=partial or aggregate_truncated,
            failed_ids=failed_ids,
            missing_fundamental_ids=missing_fundamental_ids,
        )
        activity = self._activity(components)
        capabilities = tuple(capabilities_all[:MAX_CAPABILITIES])
        status = ProjectStatus(
            generated_at=utc_now(),
            workspace_root=self._workspace_root,
            health=health,
            activity=activity,
            components=components,
            capabilities=capabilities,
            warnings=warnings,
            partial=partial or aggregate_truncated,
            failed_components=tuple(sorted(failed_ids)),
            timed_out_components=tuple(sorted(timed_out_ids)),
            omitted_components=(),
            response_truncated=aggregate_truncated,
        )
        return self._enforce_response_budget(status)

    @staticmethod
    def _health(
        components: tuple,
        *,
        partial: bool,
        failed_ids: set[str],
        missing_fundamental_ids: set[str] | frozenset[str],
    ) -> ProjectHealth:
        by_id = {component.id: component for component in components}
        if missing_fundamental_ids or failed_ids.intersection(_FUNDAMENTAL_COMPONENTS) or any(
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

    @staticmethod
    def _enforce_response_budget(status: ProjectStatus) -> ProjectStatus:
        """Omit components in reverse lexical order until UTF-8 JSON is bounded."""

        if len(status.model_dump_json().encode("utf-8")) <= MAX_STATUS_JSON_BYTES:
            return status
        retained = list(status.components)
        omitted: list[str] = []
        candidate = status
        while retained:
            omitted.append(retained.pop().id)
            capabilities = tuple(sorted(
                {capability for component in retained for capability in component.capabilities}
            )[:MAX_CAPABILITIES])
            warnings = list(status.warnings)
            if "response_truncated" not in warnings:
                warnings = (["response_truncated"] + warnings)[:MAX_WARNINGS]
            candidate = ProjectStatus(
                **{
                    **status.model_dump(),
                    "components": tuple(retained),
                    "capabilities": capabilities,
                    "warnings": tuple(warnings),
                    "partial": True,
                    "omitted_components": tuple(sorted(omitted)),
                    "response_truncated": True,
                }
            )
            if len(candidate.model_dump_json().encode("utf-8")) <= MAX_STATUS_JSON_BYTES:
                return candidate
        return candidate
