"""Builtin CMake feature plugin registered through the public plugin contract."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from importlib.resources import files
import json
from pathlib import PurePosixPath, PureWindowsPath

from pydantic import Field, ValidationError

from forgemcp.cmake.errors import CMakeRequestError
from forgemcp.cmake.service import CMakeService
from forgemcp.cmake.events import CompilationDatabaseRegistry
from forgemcp.core.errors import ForgeMCPError, to_mcp_error_response
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import (
    CompletionContribution,
    CompletionReferenceKind,
    AppCsp,
    AppResourceContribution,
    CompletionRequest,
    ForgePlugin,
    NoOpProgressReporter,
    PluginContext,
    PluginMetadata,
    ResourceContribution,
    ResourceTemplateContribution,
    ToolContribution,
    ToolHints,
    ToolExecutionContext,
    ToolAppBinding,
)
from forgemcp.project import (
    ComponentState,
    ComponentStatus,
    ProjectStatusRegistry,
    StatusFact,
)
from forgemcp.project.models import utc_now
from forgemcp.workspace import (
    WorkspaceMutationBatch,
    WorkspaceMutationBus,
    WorkspaceMutationSubscription,
    WorkspaceService,
)
from forgemcp.toolchain import ToolchainDiscoveryService


class _StatusArguments(ForgeModel):
    """CMake status deliberately accepts no options."""


class _ListPresetsArguments(ForgeModel):
    source_dir: str | None = Field(default=None, description="Optional workspace-relative source directory; configured default is used when omitted.")


class _ListKitsArguments(ForgeModel):
    """List only application-cached path-free CMake kit state."""


class _SelectKitArguments(ForgeModel):
    kit: str = Field(min_length=8, max_length=96, description="Opaque CMake kit identifier returned by cmake__list_kits.")
    expected_selection_generation: int | None = Field(default=None, ge=0, description="Optional monotonic selection generation for compare-and-swap.")


class _ListBuildTreesArguments(ForgeModel):
    source_dir: str | None = Field(default=None, description="Optional workspace-relative source directory used to assess source compatibility.")


class _ConfigureArguments(ForgeModel):
    source_dir: str | None = Field(default=None, description="Optional workspace-relative source directory; configured default is used when omitted.")
    binary_dir: str | None = Field(default=None, description="Optional workspace-relative generated build directory; resolved default is used when omitted.")
    preset: str | None = Field(default=None, description="Optional CMake configure preset name.")
    kit: str | None = Field(default=None, min_length=8, max_length=96, description="Optional opaque ForgeMCP kit identifier; cannot be combined with a preset.")
    generator: str | None = Field(default=None, max_length=256, description="Optional explicit CMake generator; cannot be combined with a preset.")
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


ToolOperation = Callable[..., Awaitable[ForgeModel]]

CMAKE_TARGETS_URI = "forgemcp://cmake/targets"
CMAKE_TARGETS_TEMPLATE_URI = "forgemcp://cmake/targets/{profile}"
CMAKE_KITS_URI = "forgemcp://cmake/kits"
CMAKE_KITS_TEMPLATE_URI = "forgemcp://cmake/kits/{kit}"
CMAKE_CATALOG_APP_URI = "ui://forgemcp/cmake/catalog"
CMAKE_OPERATION_APP_URI = "ui://forgemcp/cmake/operation"
MAX_RESOURCE_TARGETS = 512
MAX_RESOURCE_TARGET_BYTES = 220 * 1024


def _safe_relative_artifact(value: str) -> bool:
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    return (
        0 < len(value) <= 512
        and not windows.is_absolute()
        and not windows.drive
        and not windows.root
        and not posix.is_absolute()
        and ".." not in windows.parts
        and ".." not in posix.parts
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


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
        if cached.mutation_delivery_degraded and state is not ComponentState.ACTIVE:
            state = ComponentState.DEGRADED
        facts: list[StatusFact] = [
            StatusFact(name="availability_observed", value=tool is not None),
            StatusFact(name="available", value=available if available is not None else False),
            StatusFact(name="configured", value=cached.configured_binary_dir is not None),
            StatusFact(name="configuration_stale", value=cached.configuration_stale),
            StatusFact(name="mutation_delivery_degraded", value=cached.mutation_delivery_degraded),
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
        if cached.configuration_stale:
            warnings.append("configuration_stale")
        if cached.mutation_delivery_degraded:
            warnings.append("workspace_mutation_delivery_degraded")
        if cached.compilation_database is not None:
            if cached.compilation_database.availability in {"missing", "invalid", "unsupported"}:
                warnings.append("compile_commands_unavailable")
        return ComponentStatus(
            id=self.id,
            display_name="CMake and CTest",
            state=state,
            capabilities=("cmake.configure", "cmake.file-api-v2", "ctest.run"),
            summary="Cached CMake lifecycle and operation metadata; no command was executed for this snapshot.",
            facts=tuple(facts),
            warnings=tuple(warnings),
            stale=cached.configuration_stale,
            observed_at=utc_now(),
        )


class CMakePlugin(ForgePlugin):
    """Application-scoped builtin plugin exposing CMake and CTest tool contributions."""

    __slots__ = ("_service", "_status_registry", "_mutation_subscription")

    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                plugin_id="cmake",
                requires_services=("workspace", "workspace_mutations", "compilation_database", "process_runtime", "toolchain_discovery", "project_status_registry"),
                provides=frozenset({"cmake.configure", "cmake.file-api-v2", "ctest.run"}),
            )
        )
        self._service: CMakeService | None = None
        self._status_registry: ProjectStatusRegistry | None = None
        self._mutation_subscription: WorkspaceMutationSubscription | None = None

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
        mutations = context.services.get("workspace_mutations")
        compilation_database = context.services.get("compilation_database")
        status_registry = context.services.get("project_status_registry")
        if not isinstance(workspace, WorkspaceService):
            raise TypeError("The CMake plugin requires WorkspaceService under the 'workspace' service name.")
        if not isinstance(status_registry, ProjectStatusRegistry):
            raise TypeError("The CMake plugin requires ProjectStatusRegistry.")
        if not isinstance(toolchain, ToolchainDiscoveryService):
            raise TypeError("The CMake plugin requires ToolchainDiscoveryService.")
        if not isinstance(compilation_database, CompilationDatabaseRegistry):
            raise TypeError("The CMake plugin requires CompilationDatabaseRegistry.")
        if not isinstance(mutations, WorkspaceMutationBus):
            raise TypeError("The CMake plugin requires WorkspaceMutationBus.")
        self._service = CMakeService(
            workspace, process_runtime, context.config, toolchain, compilation_database, mutations
        )  # type: ignore[arg-type]
        self._mutation_subscription = mutations.subscribe("cmake", self._on_workspace_mutation)
        self._status_registry = status_registry
        status_registry.register(_CMakeStatusProvider(self._service))
        self._register_apps(context)
        context.tools.register(
            ToolContribution(
                name="status",
                description="Discover CMake and CTest and report supported parsed versions.",
                input_model=_StatusArguments,
                handler=lambda arguments, *, execution_context=None: self._dispatch(_StatusArguments, arguments, self._status, execution_context),
            )
        )
        context.resources.register(
            ResourceContribution(
                uri=CMAKE_TARGETS_URI,
                name="forgemcp_cmake_targets",
                description="Latest cached validated CMake File API target metadata; never configures or runs a process.",
                handler=lambda: self._targets_resource(None),
            )
        )
        context.resources.register(
            ResourceContribution(
                uri=CMAKE_KITS_URI,
                name="forgemcp_cmake_kits",
                description="Cached path-free CMake kit discovery; never starts a probe or process.",
                handler=lambda: self._kits_resource(None),
            )
        )
        context.resource_templates.register(
            ResourceTemplateContribution(
                uri_template=CMAKE_KITS_TEMPLATE_URI,
                name="forgemcp_cmake_kit",
                description="One cached path-free CMake kit by opaque identifier.",
                arguments=("kit",),
                handler=lambda arguments: self._kits_resource(arguments["kit"]),
            )
        )
        context.resource_templates.register(
            ResourceTemplateContribution(
                uri_template=CMAKE_TARGETS_TEMPLATE_URI,
                name="forgemcp_cmake_targets_profile",
                description="Cached validated CMake targets selected by an opaque application-local profile identifier.",
                arguments=("profile",),
                handler=lambda arguments: self._targets_resource(arguments["profile"]),
            )
        )
        self._register_completions(context)
        context.tools.register(
            ToolContribution(
                name="list_kits",
                description="List cached qualified CMake compiler kits without probing tools or reading VS Code state.",
                input_model=_ListKitsArguments,
                handler=lambda arguments, *, execution_context=None: self._dispatch(_ListKitsArguments, arguments, self._list_kits, execution_context),
            )
        )
        context.tools.register(
            ToolContribution(
                name="select_kit",
                description="Select a path-free CMake kit for this application session; no configure or filesystem deletion occurs.",
                input_model=_SelectKitArguments,
                hints=ToolHints(read_only=False, destructive=False, idempotent=False, open_world=False),
                handler=lambda arguments, *, execution_context=None: self._dispatch(_SelectKitArguments, arguments, self._select_kit, execution_context),
            )
        )
        context.tools.register(
            ToolContribution(
                name="list_build_trees",
                description="Boundedly inspect conventional existing workspace CMake build trees without configuring them.",
                input_model=_ListBuildTreesArguments,
                handler=lambda arguments, *, execution_context=None: self._dispatch(_ListBuildTreesArguments, arguments, self._list_build_trees, execution_context),
            )
        )
        context.tools.register(
            ToolContribution(
                name="list_presets",
                description="List safe configure, build, and test preset summaries without secrets.",
                input_model=_ListPresetsArguments,
                handler=lambda arguments, *, execution_context=None: self._dispatch(
                    _ListPresetsArguments, arguments, self._list_presets, execution_context
                ),
            )
        )
        context.tools.register(
            ToolContribution(
                name="configure",
                description="Configure a workspace-contained generated CMake build directory.",
                input_model=_ConfigureArguments,
                handler=lambda arguments, *, execution_context=None: self._dispatch(_ConfigureArguments, arguments, self._configure, execution_context),
            )
        )
        context.tools.register(
            ToolContribution(
                name="list_targets",
                description="Read CMake File API codemodel-v2 targets from a generated build directory.",
                input_model=_ListTargetsArguments,
                handler=lambda arguments, *, execution_context=None: self._dispatch(
                    _ListTargetsArguments, arguments, self._list_targets, execution_context
                ),
            )
        )
        context.tools.register(
            ToolContribution(
                name="build",
                description="Build a CMake project or exact target names in a safe build directory.",
                input_model=_BuildArguments,
                handler=lambda arguments, *, execution_context=None: self._dispatch(_BuildArguments, arguments, self._build, execution_context),
            )
        )
        context.tools.register(
            ToolContribution(
                name="ctest_list_tests",
                description="List CTest tests through the documented json-v1 protocol.",
                input_model=_ListTestsArguments,
                handler=lambda arguments, *, execution_context=None: self._dispatch(
                    _ListTestsArguments, arguments, self._list_tests, execution_context
                ),
            )
        )
        context.tools.register(
            ToolContribution(
                name="ctest_run",
                description="Run all CTest tests or an exact-name subset with Process Runtime limits.",
                input_model=_RunTestsArguments,
                handler=lambda arguments, *, execution_context=None: self._dispatch(_RunTestsArguments, arguments, self._run_tests, execution_context),
            )
        )

    async def stop(self) -> None:
        """Release application-owned service state; the registry removes contributions."""
        if self._mutation_subscription is not None:
            await self._mutation_subscription.aclose()
            self._mutation_subscription = None
        if self._status_registry is not None:
            self._status_registry.unregister("cmake")
            self._status_registry = None
        if self._service is not None:
            self._service.clear_selection()
        self._service = None

    @staticmethod
    def _register_apps(context: PluginContext) -> None:
        """Register static CMake result views without changing tool behaviour."""
        try:
            catalog = files("forgemcp.apps.assets").joinpath("cmake-catalog.html").read_text(encoding="utf-8")
            operation = files("forgemcp.apps.assets").joinpath("cmake-operation.html").read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError, OSError, UnicodeError) as error:
            raise RuntimeError("CMake App assets are unavailable.") from error
        csp = AppCsp(connect_domains=(), resource_domains=(), frame_domains=(), base_uri_domains=())
        context.apps.register_resource(AppResourceContribution(
            uri=CMAKE_CATALOG_APP_URI,
            name="forgemcp_cmake_catalog_app",
            description="Interactive read-only CMake catalog result view.",
            html=catalog,
            csp=csp,
            prefers_border=True,
        ))
        context.apps.register_resource(AppResourceContribution(
            uri=CMAKE_OPERATION_APP_URI,
            name="forgemcp_cmake_operation_app",
            description="Interactive read-only CMake operation result view.",
            html=operation,
            csp=csp,
            prefers_border=True,
        ))
        for tool_name in (
            "cmake__status", "cmake__list_kits", "cmake__list_build_trees", "cmake__list_presets",
            "cmake__list_targets", "cmake__ctest_list_tests",
        ):
            context.apps.bind_tool(ToolAppBinding(
                tool_name=tool_name, resource_uri=CMAKE_CATALOG_APP_URI, visibility=("model", "app")
            ))
        for tool_name in ("cmake__select_kit", "cmake__configure", "cmake__build", "cmake__ctest_run"):
            context.apps.bind_tool(ToolAppBinding(
                tool_name=tool_name, resource_uri=CMAKE_OPERATION_APP_URI, visibility=("model", "app")
            ))

    def _targets_resource(self, profile_id: str | None) -> dict[str, object]:
        if self._service is None:
            return self._targets_error("resource_unavailable")
        profile = self._service.cached_target_profile(profile_id)
        if profile is None:
            return self._targets_error(
                "profile_unavailable" if profile_id is not None else "targets_unavailable"
            )
        entries: list[dict[str, object]] = []
        omitted = 0
        serialized_bytes = 0
        configurations = sorted(
            profile.targets.configurations, key=lambda item: (item.name.casefold(), item.name)
        )
        for configuration in configurations:
            for target in sorted(
                configuration.targets, key=lambda item: (item.name.casefold(), item.name, item.type)
            ):
                if (
                    len(entries) >= MAX_RESOURCE_TARGETS
                    or len(target.name) > 256
                    or len(target.type) > 64
                    or len(configuration.name) > 128
                ):
                    omitted += 1
                    continue
                item = {
                    "configuration": configuration.name,
                    "name": target.name,
                    "type": target.type,
                    "artifacts": [
                        artifact
                        for artifact in target.artifacts[:8]
                        if _safe_relative_artifact(artifact)
                    ],
                }
                item_bytes = len(
                    json.dumps(
                        item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                )
                if serialized_bytes + item_bytes > MAX_RESOURCE_TARGET_BYTES:
                    omitted += 1
                    continue
                entries.append(item)
                serialized_bytes += item_bytes
        return {
            "schema_version": "1",
            "resource": CMAKE_TARGETS_URI,
            "state": "stale" if self._service.cached_targets_stale else "available",
            "profile": profile.profile_id,
            "observed_at": profile.observed_at.isoformat(),
            "targets": entries,
            "complete": omitted == 0,
            "truncated": omitted > 0,
            "omitted_target_count": omitted,
        }

    def _kits_resource(self, kit_id: str | None) -> dict[str, object]:
        if self._service is None:
            return {
                "schema_version": "1", "resource": CMAKE_KITS_URI,
                "state": "unavailable", "kits": [], "complete": False,
                "error": {"code": "resource_unavailable", "message": "Cached CMake kits are unavailable."},
            }
        listing = self._service.list_kits()
        if kit_id is not None:
            kit = next((item for item in listing.kits if item.id == kit_id), None)
            if kit is None:
                return {
                    "schema_version": "1", "resource": CMAKE_KITS_URI,
                    "state": "unavailable", "kits": [], "complete": False,
                    "error": {"code": "kit_unavailable", "message": "The requested cached CMake kit is unavailable."},
                }
            return {
                "schema_version": "1", "resource": CMAKE_KITS_URI, "state": "available",
                "kit": kit.model_dump(mode="json"), "complete": True,
            }
        return {
            "schema_version": "1", "resource": CMAKE_KITS_URI,
            "state": listing.discovery_state, "kits": [item.model_dump(mode="json") for item in listing.kits],
            "complete": listing.complete,
        }

    @staticmethod
    def _targets_error(code: str) -> dict[str, object]:
        return {
            "schema_version": "1",
            "resource": CMAKE_TARGETS_URI,
            "state": "unavailable",
            "targets": [],
            "complete": False,
            "truncated": False,
            "error": {"code": code, "message": "Cached validated CMake targets are unavailable."},
        }

    def _register_completions(self, context: PluginContext) -> None:
        profile_references = (
            "forgemcp_build_report",
            "forgemcp_test_report",
            "forgemcp_diagnose_build",
            "forgemcp_debug_target",
        )
        for reference in profile_references:
            context.completions.register(
                CompletionContribution(
                    reference_kind=CompletionReferenceKind.PROMPT,
                    reference=reference,
                    argument="profile",
                    provider=self._complete_profiles,
                )
            )
            context.completions.register(
                CompletionContribution(
                    reference_kind=CompletionReferenceKind.PROMPT,
                    reference=reference,
                    argument="configuration",
                    provider=self._complete_configurations,
                )
            )
            context.completions.register(
                CompletionContribution(
                    reference_kind=CompletionReferenceKind.PROMPT,
                    reference=reference,
                    argument="generator",
                    provider=self._complete_generators,
                )
            )
        for reference in (
            "forgemcp_build_report",
            "forgemcp_test_report",
            "forgemcp_diagnose_build",
        ):
            context.completions.register(
                CompletionContribution(
                    reference_kind=CompletionReferenceKind.PROMPT,
                    reference=reference,
                    argument="preset",
                    provider=self._complete_presets,
                )
            )
        for reference in (
            "forgemcp_build_report",
            "forgemcp_diagnose_build",
            "forgemcp_debug_target",
        ):
            context.completions.register(
                CompletionContribution(
                    reference_kind=CompletionReferenceKind.PROMPT,
                    reference=reference,
                    argument="target",
                    provider=self._complete_targets,
                )
            )
        context.completions.register(
            CompletionContribution(
                reference_kind=CompletionReferenceKind.PROMPT,
                reference="forgemcp_test_report",
                argument="test",
                provider=self._complete_tests,
            )
        )
        context.completions.register(
            CompletionContribution(
                reference_kind=CompletionReferenceKind.RESOURCE_TEMPLATE,
                reference=CMAKE_KITS_TEMPLATE_URI,
                argument="kit",
                provider=self._complete_kits,
            )
        )
        for reference in profile_references:
            context.completions.register(
                CompletionContribution(
                    reference_kind=CompletionReferenceKind.PROMPT,
                    reference=reference,
                    argument="kit",
                    provider=self._complete_kits,
                )
            )
        context.completions.register(
            CompletionContribution(
                reference_kind=CompletionReferenceKind.RESOURCE_TEMPLATE,
                reference=CMAKE_TARGETS_TEMPLATE_URI,
                argument="profile",
                provider=self._complete_profiles,
            )
        )

    def _complete_profiles(self, request: CompletionRequest) -> tuple[str, ...]:
        if self._service is None:
            return ()
        return self._service.cached_profile_ids(compatible_kit=request.context.get("kit"))

    def _complete_kits(self, _request: CompletionRequest) -> tuple[str, ...]:
        if self._service is None:
            return ()
        return tuple(item.id for item in self._service.list_kits().kits if item.readiness != "rejected")

    def _complete_generators(self, request: CompletionRequest) -> tuple[str, ...]:
        if self._service is None:
            return ()
        return self._service.compatible_generators(request.context.get("kit"))

    async def _complete_presets(self, _request: CompletionRequest) -> tuple[str, ...]:
        if self._service is None:
            return ()
        if not self._service.cached_preset_names():
            try:
                await self._service.list_presets()
            except ForgeMCPError:
                return ()
        return self._service.cached_preset_names()

    def _complete_configurations(self, request: CompletionRequest) -> tuple[str, ...]:
        if self._service is None:
            return ()
        return self._service.cached_configurations(request.context.get("profile"))

    def _complete_targets(self, request: CompletionRequest) -> tuple[str, ...]:
        if self._service is None:
            return ()
        return self._service.cached_target_names(
            request.context.get("profile"), request.context.get("configuration")
        )

    def _complete_tests(self, request: CompletionRequest) -> tuple[str, ...]:
        if self._service is None:
            return ()
        return self._service.cached_test_names(request.context.get("profile"))

    def _on_workspace_mutation(self, batch: WorkspaceMutationBatch) -> None:
        """Mark cached CMake state stale after a committed source transaction."""
        if self._service is not None:
            self._service.mark_workspace_mutation(
                tuple(change.path for change in batch.changes), generation=batch.generation
            )

    async def _dispatch(
        self,
        model_type: type[ForgeModel],
        arguments: Mapping[str, object],
        operation: ToolOperation,
        execution_context: ToolExecutionContext | None = None,
    ) -> dict[str, object]:
        try:
            request = model_type.model_validate(arguments)
        except ValidationError:
            return to_mcp_error_response(
                CMakeRequestError("Tool arguments do not match the published CMake schema.")
            ).as_dict()
        try:
            result = await operation(self.service, request, execution_context or ToolExecutionContext(NoOpProgressReporter()))
        except ForgeMCPError as error:
            return to_mcp_error_response(error).as_dict()
        return result.model_dump(mode="json")

    @staticmethod
    async def _status(service: CMakeService, _: ForgeModel, __: ToolExecutionContext) -> ForgeModel:
        return await service.status()

    @staticmethod
    async def _list_presets(service: CMakeService, request: ForgeModel, __: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _ListPresetsArguments)
        return await service.list_presets(source_dir=request.source_dir)

    @staticmethod
    async def _configure(service: CMakeService, request: ForgeModel, execution_context: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _ConfigureArguments)
        return await service.configure(
            source_dir=request.source_dir,
            binary_dir=request.binary_dir,
            preset=request.preset,
            kit=request.kit,
            generator=request.generator,
            cache_variables=request.cache_variables,
            execution_context=execution_context,
        )

    @staticmethod
    async def _list_kits(service: CMakeService, _: ForgeModel, __: ToolExecutionContext) -> ForgeModel:
        return service.list_kits()

    @staticmethod
    async def _select_kit(service: CMakeService, request: ForgeModel, __: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _SelectKitArguments)
        return await service.select_kit(
            request.kit, expected_selection_generation=request.expected_selection_generation
        )

    @staticmethod
    async def _list_build_trees(service: CMakeService, request: ForgeModel, __: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _ListBuildTreesArguments)
        # Generic tool output remains a strict Pydantic model; package the
        # bounded immutable values in a tiny local result model at dispatch.
        from forgemcp.cmake.models import CMakeBuildTreeList
        return CMakeBuildTreeList(build_trees=service.list_build_trees(source_dir=request.source_dir))

    @staticmethod
    async def _list_targets(service: CMakeService, request: ForgeModel, __: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _ListTargetsArguments)
        return service.list_targets(binary_dir=request.binary_dir)

    @staticmethod
    async def _build(service: CMakeService, request: ForgeModel, execution_context: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _BuildArguments)
        return await service.build(
            binary_dir=request.binary_dir,
            targets=request.targets,
            configuration=request.configuration,
            parallel_jobs=request.parallel_jobs,
            execution_context=execution_context,
        )

    @staticmethod
    async def _list_tests(service: CMakeService, request: ForgeModel, __: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _ListTestsArguments)
        return await service.list_tests(binary_dir=request.binary_dir)

    @staticmethod
    async def _run_tests(service: CMakeService, request: ForgeModel, execution_context: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _RunTestsArguments)
        return await service.run_tests(
            binary_dir=request.binary_dir,
            test_names=request.test_names,
            configuration=request.configuration,
            timeout_seconds=request.timeout_seconds,
            execution_context=execution_context,
        )
