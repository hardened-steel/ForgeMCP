"""Builtin CMake feature plugin registered through the public plugin contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from pydantic import Field, ValidationError

from forgemcp.cmake.errors import CMakeRequestError
from forgemcp.cmake.service import CMakeService
from forgemcp.core.errors import ForgeMCPError, to_mcp_error_response
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import ForgePlugin, PluginContext, PluginMetadata, ToolContribution
from forgemcp.workspace import WorkspaceService


class _StatusArguments(ForgeModel):
    """CMake status deliberately accepts no options."""


class _ListPresetsArguments(ForgeModel):
    source_dir: str = Field(default=".", description="Workspace-relative source directory.")


class _ConfigureArguments(ForgeModel):
    source_dir: str = Field(default=".", description="Workspace-relative source directory.")
    binary_dir: str = Field(description="Workspace-relative generated build directory.")
    preset: str | None = Field(default=None, description="Optional CMake configure preset name.")
    cache_variables: dict[str, str | int | bool] | None = Field(
        default=None,
        description="Optional validated scalar CMake cache values; environment values are not accepted.",
    )


class _ListTargetsArguments(ForgeModel):
    binary_dir: str = Field(description="Workspace-relative generated build directory.")


class _BuildArguments(ForgeModel):
    binary_dir: str = Field(description="Workspace-relative generated build directory.")
    targets: list[str] = Field(default_factory=list, description="Optional exact CMake target names.")
    configuration: str | None = Field(default=None, description="Optional multi-config configuration.")
    parallel_jobs: int | None = Field(default=None, description="Optional bounded positive parallel-job count.")


class _ListTestsArguments(ForgeModel):
    binary_dir: str = Field(description="Workspace-relative generated build directory.")


class _RunTestsArguments(ForgeModel):
    binary_dir: str = Field(description="Workspace-relative generated build directory.")
    test_names: list[str] = Field(default_factory=list, description="Optional exact CTest test names.")
    configuration: str | None = Field(default=None, description="Optional multi-config configuration.")
    timeout_seconds: float | None = Field(
        default=None,
        description="Optional timeout constrained by the configured Process Runtime policy.",
    )


ToolOperation = Callable[[CMakeService, ForgeModel], Awaitable[ForgeModel]]


class CMakePlugin(ForgePlugin):
    """Application-scoped builtin plugin exposing CMake and CTest tool contributions."""

    __slots__ = ("_service",)

    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                plugin_id="cmake",
                requires_services=("workspace", "process_runtime"),
                provides=frozenset({"cmake.configure", "cmake.file-api-v2", "ctest.run"}),
            )
        )
        self._service: CMakeService | None = None

    @property
    def service(self) -> CMakeService:
        """Return this application's CMake service while the plugin is running."""
        if self._service is None:
            raise RuntimeError("The CMake plugin is not running.")
        return self._service

    async def start(self, context: PluginContext) -> None:
        workspace = context.services.get("workspace")
        process_runtime = context.services.get("process_runtime")
        if not isinstance(workspace, WorkspaceService):
            raise TypeError("The CMake plugin requires WorkspaceService under the 'workspace' service name.")
        self._service = CMakeService(workspace, process_runtime)  # type: ignore[arg-type]
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
