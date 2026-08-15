"""Builtin CMake feature plugin registered through the public plugin contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from pydantic import Field, ValidationError

from forgemcp.cmake.errors import CMakeRequestError
from forgemcp.cmake.service import CMakeService
from forgemcp.core.errors import ForgeMCPError, to_mcp_error_response
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import ForgePlugin, PluginContext, PluginMetadata, ToolContribution
from forgemcp.project import (
    ComponentState,
    ComponentStatus,
    ProjectStatusRegistry,
    StatusFact,
)
from forgemcp.project.models import utc_now
from forgemcp.workspace import WorkspaceService
from forgemcp.toolchain import ToolchainDiscoveryService


class _StatusArguments(ForgeModel):
    """CMake status deliberately accepts no options."""


class _ListPresetsArguments(ForgeModel):
    source_dir: str | None = Field(default=None, description="Optional workspace-relative source directory; configured default is used when omitted.")


class _ConfigureArguments(ForgeModel):
    source_dir: str | None = Field(default=None, description="Optional workspace-relative source directory; configured default is used when omitted.")
    binary_dir: str | None = Field(default=None, description="Optional workspace-relative generated build directory; resolved default is used when omitted.")
    preset: str | None = Field(default=None, description="Optional CMake configure preset name.")
    cache_variables: dict[str, str | int | bool] | None = Field(
        default=None,
        description="Optional validated scalar CMake cache values; environment values are not accepted.",
    )


class _ListTargetsArguments(ForgeModel):
    binary_dir: str | None = Field(default=None, description="Optional workspace-relative generated build directory.")


class _BuildArguments(ForgeModel):
    binary_dir: str | None = Field(default=None, description="Optional workspace-relative generated build directory.")
    targets: list[str] = Field(default_factory=list, description="Optional exact CMake target names.")
    configuration: str | None = Field(default=None, description="Optional multi-config configuration.")
    parallel_jobs: int | None = Field(default=None, description="Optional bounded positive parallel-job count.")


class _ListTestsArguments(ForgeModel):
    binary_dir: str | None = Field(default=None, description="Optional workspace-relative generated build directory.")


class _RunTestsArguments(ForgeModel):
    binary_dir: str | None = Field(default=None, description="Optional workspace-relative generated build directory.")
    test_names: list[str] = Field(default_factory=list, description="Optional exact CTest test names.")
    configuration: str | None = Field(default=None, description="Optional multi-config configuration.")
    timeout_seconds: float | None = Field(
        default=None,
        description="Optional timeout constrained by the configured Process Runtime policy.",
    )


ToolOperation = Callable[[CMakeService, ForgeModel], Awaitable[ForgeModel]]


class _CMakeStatusProvider:
    id = "cmake"

    def __init__(self, service: CMakeService) -> None:
        self._service = service

    async def snapshot_status(self) -> ComponentStatus:
        cached = self._service.cached_project_status()
        tool = cached.tool_status
        available = tool.available if tool is not None else None
        state = (
            ComponentState.ACTIVE
            if cached.active_operations
            else ComponentState.UNAVAILABLE
            if available is False
            else ComponentState.IDLE
        )
        facts: list[StatusFact] = [
            StatusFact(name="availability_observed", value=tool is not None),
            StatusFact(name="available", value=available if available is not None else False),
            StatusFact(name="configured", value=cached.configured_binary_dir is not None),
            StatusFact(name="active_operations", value=cached.active_operations),
        ]
        warnings: list[str] = []
        if tool is not None and tool.cmake.version is not None:
            facts.append(StatusFact(name="version", value=tool.cmake.version.full))
        if cached.configured_binary_dir is not None:
            if len(cached.configured_binary_dir) <= 256:
                facts.append(StatusFact(name="build_directory", value=cached.configured_binary_dir))
            else:
                warnings.append("workspace_relative_path_omitted")
        for operation in (cached.last_configure, cached.last_build, cached.last_test):
            if operation is None:
                continue
            prefix = f"last_{operation.operation}"
            facts.extend(
                (
                    StatusFact(name=f"{prefix}_outcome", value=operation.outcome),
                    StatusFact(name=f"{prefix}_duration", value=operation.duration_milliseconds, unit="milliseconds"),
                    StatusFact(name=f"{prefix}_item_count", value=operation.item_count),
                    StatusFact(name=f"{prefix}_observed_at", value=operation.observed_at.isoformat()),
                )
            )
            if len(operation.binary_dir) <= 256:
                facts.append(StatusFact(name=f"{prefix}_directory", value=operation.binary_dir))
            else:
                warnings.append("workspace_relative_path_omitted")
            if operation.exit_code is not None:
                facts.append(StatusFact(name=f"{prefix}_exit_code", value=operation.exit_code))
            if operation.outcome != "success":
                warnings.append(f"{prefix}_{operation.outcome}")
        if tool is None:
            warnings.append("tool_availability_not_observed")
        return ComponentStatus(
            id=self.id,
            display_name="CMake and CTest",
            state=state,
            capabilities=("cmake.configure", "cmake.file-api-v2", "ctest.run"),
            summary="Cached CMake lifecycle and operation metadata; no command was executed for this snapshot.",
            facts=tuple(facts),
            warnings=tuple(warnings),
            observed_at=utc_now(),
        )


class CMakePlugin(ForgePlugin):
    """Application-scoped builtin plugin exposing CMake and CTest tool contributions."""

    __slots__ = ("_service", "_status_registry")

    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                plugin_id="cmake",
                requires_services=("workspace", "process_runtime", "toolchain_discovery", "project_status_registry"),
                provides=frozenset({"cmake.configure", "cmake.file-api-v2", "ctest.run"}),
            )
        )
        self._service: CMakeService | None = None
        self._status_registry: ProjectStatusRegistry | None = None

    @property
    def service(self) -> CMakeService:
        """Return this application's CMake service while the plugin is running."""
        if self._service is None:
            raise RuntimeError("The CMake plugin is not running.")
        return self._service

    async def start(self, context: PluginContext) -> None:
        workspace = context.services.get("workspace")
        process_runtime = context.services.get("process_runtime")
        toolchain = context.services.get("toolchain_discovery")
        status_registry = context.services.get("project_status_registry")
        if not isinstance(workspace, WorkspaceService):
            raise TypeError("The CMake plugin requires WorkspaceService under the 'workspace' service name.")
        if not isinstance(status_registry, ProjectStatusRegistry):
            raise TypeError("The CMake plugin requires ProjectStatusRegistry.")
        if not isinstance(toolchain, ToolchainDiscoveryService):
            raise TypeError("The CMake plugin requires ToolchainDiscoveryService.")
        self._service = CMakeService(workspace, process_runtime, context.config, toolchain)  # type: ignore[arg-type]
        self._status_registry = status_registry
        status_registry.register(_CMakeStatusProvider(self._service))
        context.tools.register(
            ToolContribution(
                name="status",
                description="Discover CMake and CTest and report supported parsed versions.",
                input_model=_StatusArguments,
                handler=lambda arguments: self._dispatch(_StatusArguments, arguments, self._status),
            )
        )
        context.tools.register(
            ToolContribution(
                name="list_presets",
                description="List safe configure, build, and test preset summaries without secrets.",
                input_model=_ListPresetsArguments,
                handler=lambda arguments: self._dispatch(
                    _ListPresetsArguments, arguments, self._list_presets
                ),
            )
        )
        context.tools.register(
            ToolContribution(
                name="configure",
                description="Configure a workspace-contained generated CMake build directory.",
                input_model=_ConfigureArguments,
                handler=lambda arguments: self._dispatch(_ConfigureArguments, arguments, self._configure),
            )
        )
        context.tools.register(
            ToolContribution(
                name="list_targets",
                description="Read CMake File API codemodel-v2 targets from a generated build directory.",
                input_model=_ListTargetsArguments,
                handler=lambda arguments: self._dispatch(
                    _ListTargetsArguments, arguments, self._list_targets
                ),
            )
        )
        context.tools.register(
            ToolContribution(
                name="build",
                description="Build a CMake project or exact target names in a safe build directory.",
                input_model=_BuildArguments,
                handler=lambda arguments: self._dispatch(_BuildArguments, arguments, self._build),
            )
        )
        context.tools.register(
            ToolContribution(
                name="ctest_list_tests",
                description="List CTest tests through the documented json-v1 protocol.",
                input_model=_ListTestsArguments,
                handler=lambda arguments: self._dispatch(
                    _ListTestsArguments, arguments, self._list_tests
                ),
            )
        )
        context.tools.register(
            ToolContribution(
                name="ctest_run",
                description="Run all CTest tests or an exact-name subset with Process Runtime limits.",
                input_model=_RunTestsArguments,
                handler=lambda arguments: self._dispatch(_RunTestsArguments, arguments, self._run_tests),
            )
        )

    async def stop(self) -> None:
        """Release application-owned service state; the registry removes contributions."""
        if self._status_registry is not None:
            self._status_registry.unregister("cmake")
            self._status_registry = None
        self._service = None

    async def _dispatch(
        self,
        model_type: type[ForgeModel],
        arguments: Mapping[str, object],
        operation: ToolOperation,
    ) -> dict[str, object]:
        try:
            request = model_type.model_validate(arguments)
        except ValidationError:
            return to_mcp_error_response(
                CMakeRequestError("Tool arguments do not match the published CMake schema.")
            ).as_dict()
        try:
            result = await operation(self.service, request)
        except ForgeMCPError as error:
            return to_mcp_error_response(error).as_dict()
        return result.model_dump(mode="json")

    @staticmethod
    async def _status(service: CMakeService, _: ForgeModel) -> ForgeModel:
        return await service.status()

    @staticmethod
    async def _list_presets(service: CMakeService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _ListPresetsArguments)
        return await service.list_presets(source_dir=request.source_dir)

    @staticmethod
    async def _configure(service: CMakeService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _ConfigureArguments)
        return await service.configure(
            source_dir=request.source_dir,
            binary_dir=request.binary_dir,
            preset=request.preset,
            cache_variables=request.cache_variables,
        )

    @staticmethod
    async def _list_targets(service: CMakeService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _ListTargetsArguments)
        return service.list_targets(binary_dir=request.binary_dir)

    @staticmethod
    async def _build(service: CMakeService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _BuildArguments)
        return await service.build(
            binary_dir=request.binary_dir,
            targets=request.targets,
            configuration=request.configuration,
            parallel_jobs=request.parallel_jobs,
        )

    @staticmethod
    async def _list_tests(service: CMakeService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _ListTestsArguments)
        return await service.list_tests(binary_dir=request.binary_dir)

    @staticmethod
    async def _run_tests(service: CMakeService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _RunTestsArguments)
        return await service.run_tests(
            binary_dir=request.binary_dir,
            test_names=request.test_names,
            configuration=request.configuration,
            timeout_seconds=request.timeout_seconds,
        )
