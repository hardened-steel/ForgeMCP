"""ToolContribution and foundational cached providers for project status."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic import ValidationError

from forgemcp import __version__
from forgemcp.core.errors import to_mcp_error_response
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import ForgePlugin, PluginContext, PluginManager, PluginMetadata, ToolContribution
from forgemcp.processes import ProcessRuntime
from forgemcp.project.errors import ProjectStatusError, ProjectStatusRequestError
from forgemcp.project.models import MAX_CAPABILITIES, ComponentState, ComponentStatus, StatusFact, utc_now
from forgemcp.project.registry import ProjectStatusProvider, ProjectStatusRegistry
from forgemcp.project.service import ProjectStatusService
from forgemcp.workspace import WorkspaceService


class _ProjectStatusArguments(ForgeModel):
    """Project Intelligence Phase 1 deliberately accepts no options."""


class CoreStatusProvider:
    """Cached application lifecycle adapter; it performs no inspection or probes."""

    id = "core"

    def __init__(self, state: Callable[[], str], *, transport: str = "stdio") -> None:
        self._state = state
        self._transport = transport

    async def snapshot_status(self) -> ComponentStatus:
        state = self._state()
        return ComponentStatus(
            id=self.id,
            display_name="ForgeMCP Core",
            state=ComponentState.ACTIVE if state == "running" else ComponentState.STOPPED,
            capabilities=("mcp.tools", "project.status"),
            summary="ForgeMCP application lifecycle cache is available.",
            facts=(
                StatusFact(name="version", value=__version__),
                StatusFact(name="transport", value=self._transport),
                StatusFact(name="lifecycle", value=state),
            ),
            observed_at=utc_now(),
        )


class WorkspaceStatusProvider:
    """Expose immutable Workspace policy already held in memory."""

    id = "workspace"

    def __init__(self, workspace: WorkspaceService) -> None:
        self._workspace = workspace

    async def snapshot_status(self) -> ComponentStatus:
        policy = self._workspace.policy
        return ComponentStatus(
            id=self.id,
            display_name="Workspace",
            state=ComponentState.AVAILABLE,
            capabilities=("workspace.read", "workspace.patch", "workspace.generated_directory"),
            summary="Configured workspace root and immutable access policy are available.",
            facts=(
                StatusFact(name="read_policy", value=True),
                StatusFact(name="write_policy", value=True),
                StatusFact(name="max_read", value=policy.max_read_bytes, unit="bytes"),
                StatusFact(name="max_patch", value=policy.max_patch_bytes, unit="bytes"),
            ),
            observed_at=utc_now(),
        )


class ProcessRuntimeStatusProvider:
    """Expose only cached runtime counters and policy modes."""

    id = "process_runtime"

    def __init__(self, runtime: ProcessRuntime) -> None:
        self._runtime = runtime

    async def snapshot_status(self) -> ComponentStatus:
        cached = self._runtime.cached_status()
        return ComponentStatus(
            id=self.id,
            display_name="Process Runtime",
            state=ComponentState.STOPPED if cached.closed else ComponentState.AVAILABLE,
            capabilities=("process.bounded", "process.persistent", "process.tree_ownership"),
            summary="Process lifecycle counters and immutable ownership policy are available.",
            facts=(
                StatusFact(name="active_processes", value=cached.active_processes),
                StatusFact(name="active_persistent_adapters", value=cached.active_persistent_adapters),
                StatusFact(name="ownership_modes", value="best_effort,required"),
                StatusFact(name="best_effort_ownership", value=cached.best_effort_ownership),
                StatusFact(name="required_ownership", value=cached.required_ownership),
            ),
            observed_at=utc_now(),
        )


class PluginManagerStatusProvider:
    """Normalize the manager's already-cached records without module paths."""

    id = "plugin_manager"

    def __init__(self, manager: PluginManager, *, external_enabled: bool) -> None:
        self._manager = manager
        self._external_enabled = external_enabled

    async def snapshot_status(self) -> ComponentStatus:
        statuses = self._manager.statuses()
        counts = {name: 0 for name in ("running", "failed", "stopped", "starting", "registered")}
        for status in statuses:
            counts[status.state.value] += 1
        failed = counts["failed"] > 0
        declared_capabilities = {capability for status in statuses for capability in status.provides}
        safe_capabilities = sorted(
            capability
            for capability in declared_capabilities
            if len(capability) <= 64
            and capability[0] in "abcdefghijklmnopqrstuvwxyz"
            and all(character in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for character in capability)
        )
        capabilities = tuple(safe_capabilities[:MAX_CAPABILITIES])
        omitted_capability_count = len(declared_capabilities) - len(capabilities)
        warnings = []
        if failed:
            warnings.append("plugin_startup_failure")
        if omitted_capability_count:
            warnings.append("capabilities_truncated")
        return ComponentStatus(
            id=self.id,
            display_name="Plugin Manager",
            state=ComponentState.FAILED if failed else ComponentState.AVAILABLE,
            capabilities=capabilities,
            summary="Plugin lifecycle and declared capability cache is available.",
            facts=(
                StatusFact(name="running_plugins", value=counts["running"]),
                StatusFact(name="failed_plugins", value=counts["failed"]),
                StatusFact(name="stopped_plugins", value=counts["stopped"]),
                StatusFact(name="external_plugins_enabled", value=self._external_enabled),
                StatusFact(name="omitted_capabilities", value=omitted_capability_count),
            ),
            warnings=tuple(warnings),
            observed_at=utc_now(),
        )


class ProjectPlugin(ForgePlugin):
    """Application-owned contributor of the sole project__status MCP tool."""

    __slots__ = ("_service",)

    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                plugin_id="project",
                requires_services=("project_status_service",),
                provides=frozenset({"project.status"}),
            )
        )
        self._service: ProjectStatusService | None = None

    async def start(self, context: PluginContext) -> None:
        service = context.services.get("project_status_service")
        if not isinstance(service, ProjectStatusService):
            raise TypeError("ProjectPlugin requires ProjectStatusService.")
        self._service = service
        context.tools.register(
            ToolContribution(
                name="status",
                description="Return a side-effect-free bounded aggregation of cached component status.",
                input_model=_ProjectStatusArguments,
                handler=self._dispatch,
            )
        )

    async def stop(self) -> None:
        self._service = None

    async def _dispatch(self, arguments: Mapping[str, object]) -> dict[str, object]:
        try:
            _ProjectStatusArguments.model_validate(arguments)
        except ValidationError:
            return to_mcp_error_response(
                ProjectStatusRequestError("project__status accepts no fields.")
            ).as_dict()
        if self._service is None:
            return to_mcp_error_response(
                ProjectStatusError("Project status is unavailable during shutdown.")
            ).as_dict()
        try:
            status = await self._service.status()
        except ProjectStatusError as error:
            return to_mcp_error_response(error).as_dict()
        return status.model_dump(mode="json")
