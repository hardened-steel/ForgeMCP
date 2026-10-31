"""CMake profile selection, build-tree ownership, and MCP operations."""

from __future__ import annotations

import asyncio
import tempfile
from collections import defaultdict
from collections.abc import AsyncGenerator, Sequence
from pathlib import Path
from typing import Annotated, cast
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from forgemcp.assets import IconFile, Widget
from forgemcp.completion import Complete
from forgemcp.diagnostics import available_values, display_value, os_error_reason
from forgemcp.process.errors import ProcessError, ProcessStartError
from forgemcp.process.models import ProcessTimeout
from forgemcp.process.service import ProcessService
from forgemcp.progress import progress
from forgemcp.text import tool_result
from forgemcp.toolchain.errors import ToolchainError
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.spec import Toolset
from forgemcp.toolchain.tools import cmake, ctest
from forgemcp.workspace.path import WorkspacePath

from .build_tree import (
    build_path,
    clear_build_directory,
    execution_paths,
    read_cache,
    require_configured,
)
from .errors import CMakeError
from .file_api import (
    ConfigurationInputs,
    WorkspaceChange,
    compilation_database,
    generation,
    input_checksums,
    input_path,
    path_kind,
    prepare_queries,
    read_generation,
)
from .profiles import (
    ProfileDefinition,
    configure_definitions,
    operation_presets,
    plain_profile,
    require_configuration,
    validate_names,
    validate_presets,
)
from .models import (
    CMakeProfile,
    CompilationContext,
    CMakeConfigureResult,
    CMakeBuildResult,
    CMakeTestResult,
    CMakeRunTestResult,
)
from .text import render_executions, render_profiles, render_run_test


class CMakeService:
    """CMake: configuration, builds, and CTest runs through named operator profiles.

    Each profile binds a toolset to manual settings (configuration, generator,
    compilers, build directory, cache definitions) or native presets.
    Configure/build/test presets are selected independently; no associations are
    inferred. Profile settings belong to the operator, not tool arguments.

    When the operator supplies no profiles, preset files yield one presets profile
    running all available presets; without preset files, Debug and Release use
    separate storage/build/cmake-<name> directories.
    Omitting the profiles filter runs all profiles; a nonempty list selects a subset.
    Configure before building; build before testing. Diagnose with a subset,
    then verify all profiles. Each execution reports error (null means success)
    and, when launched, process_id for its command log.

    Workspace changes to configuration inputs withdraw affected clangd contexts.
    Run cmake_configure to confirm unknown or outdated configurations. A build
    may restore them through CMake's automatic generation. External edits are not watched.

    cmake_run_test runs an explicit project/storage test executable with literal
    arguments and a working directory, inheriting the server environment.
    It requires no profile or CTest registration. Zero exit means success;
    CTest properties, preset environments, and framework case reports do not apply.
    """

    ICON = IconFile("icons/cmake.svg")
    PROFILES_WIDGET = Widget("assets/cmake-profiles.html")
    CONFIGURE_WIDGET = Widget("assets/cmake-configure.html")
    BUILD_WIDGET = Widget("assets/cmake-build.html")
    TEST_WIDGET = Widget("assets/cmake-test.html")
    RUN_TEST_WIDGET = Widget("assets/cmake-run-test.html")

    def __init__(
        self,
        toolchains: ToolchainService,
        processes: ProcessService,
        profiles: tuple[ProfileDefinition, ...] = (),
        *,
        project_root: Path,
        storage_root: Path,
        protected_paths: set[Path],
        default_toolset: str = "system",
        progress_interval: float = 1.0,
    ) -> None:
        """Bind toolchains and managed roots with empty configuration and subscription state."""
        self.project_root = project_root.resolve()
        self.storage_root = storage_root.resolve()
        self.protected_paths = protected_paths
        self.toolchains = toolchains
        self.processes = processes
        self.definitions = profiles
        self.default_toolset = default_toolset
        self.progress_interval = progress_interval
        self.configurations: dict[str, CompilationContext] = {}
        self.inputs: defaultdict[WorkspacePath, ConfigurationInputs] = defaultdict(
            ConfigurationInputs
        )
        self.operation_lock = asyncio.Lock()
        self.configuration_subscribers: dict[
            asyncio.Queue[list[CompilationContext]], tuple[str, ...]
        ] = {}

    def toolset(self, selector: str) -> Toolset:
        """Resolve a toolset name or ID and require exactly one match."""
        matches = [
            item
            for item in self.toolchains.list_toolsets()
            if selector in (item.id, item.name)
        ]
        if len(matches) != 1:
            choices = [item.id for item in matches or self.toolchains.list_toolsets()]
            reason = "matches multiple toolsets" if matches else "matches no toolset"
            raise CMakeError(
                f"Operator toolset setting {display_value(selector)} {reason}. "
                "Select one exact toolset ID in --cmake-toolset or --cmake-profile. "
                + available_values(choices, "toolsets_list()")
            )
        return matches[0]

    def cmake_methods(self, toolset_id: str) -> cmake.Methods:
        """Return typed CMake operations or reject a toolset without CMake."""
        tool = self.toolchains.get_tool(toolset_id, "cmake")
        if tool is None:
            raise CMakeError(
                f"Toolset {display_value(toolset_id)} has no cmake executable. "
                f"Inspect toolset_get(toolset_id={display_value(toolset_id)}); "
                "the operator must install CMake or select a toolset containing it."
            )
        return cast(cmake.Methods, tool.methods)

    def ctest_methods(self, toolset_id: str) -> ctest.Methods:
        """Return typed CTest operations or reject a toolset without CTest."""
        tool = self.toolchains.get_tool(toolset_id, "ctest")
        if tool is None:
            raise CMakeError(
                f"Toolset {display_value(toolset_id)} has no ctest executable. "
                f"Inspect toolset_get(toolset_id={display_value(toolset_id)}); "
                "the operator must install CTest or select a toolset containing it."
            )
        return cast(ctest.Methods, tool.methods)

    async def available_presets(self, toolset_id: str) -> cmake.PresetsResult:
        """Ask the selected CMake executable for the project's preset catalog."""
        methods = self.cmake_methods(toolset_id)
        return await methods["presets"](self.project_root)


    async def selection(
        self,
        names: list[str] | None,
    ) -> tuple[list[ProfileDefinition], dict[str, cmake.PresetsResult | CMakeError]]:
        """Choose explicit or automatic profiles and collect their available preset catalogs."""
        catalogs = {}
        if self.definitions:
            definitions = list(self.definitions)
        elif any(
            (self.project_root / filename).is_file()
            for filename in ("CMakePresets.json", "CMakeUserPresets.json")
        ):
            toolset = self.toolset(self.default_toolset)
            available = await self.available_presets(toolset.id)
            catalogs[toolset.id] = available
            definitions = [
                ProfileDefinition(
                    name="presets",
                    toolset=toolset.id,
                    configure_presets=available.configure,
                    build_presets=available.build,
                    test_presets=available.test,
                )
            ]
        else:
            definitions = [
                ProfileDefinition(
                    name=configuration,
                    toolset=self.default_toolset,
                    configuration=configuration,
                )
                for configuration in ("Debug", "Release")
            ]
        if names is not None:
            if not names:
                raise CMakeError(
                    "profiles=[] is empty. Omit profiles or pass null to select all profiles. "
                    + available_values(
                        [definition.name for definition in definitions],
                        "cmake_profiles()",
                    )
                )
            for index, name in enumerate(names):
                if name in names[:index]:
                    raise CMakeError(
                        f"profiles[{index}]={display_value(name)} duplicates "
                        f"profiles[{names.index(name)}]. List each profile once."
                    )
            unknown = set(names) - {definition.name for definition in definitions}
            if unknown:
                invalid = next(index for index, name in enumerate(names) if name in unknown)
                raise CMakeError(
                    f"Unknown CMake profiles: profiles[{invalid}]={display_value(names[invalid])}. "
                    + available_values(
                        [definition.name for definition in definitions],
                        "cmake_profiles()",
                    )
                )
            definitions = [definition for definition in definitions if definition.name in names]
        for definition in definitions:
            try:
                toolset = self.toolset(definition.toolset)
            except CMakeError:
                # Report a bad toolset on its own profile without stopping the others.
                continue
            if definition.uses_presets and toolset.id not in catalogs:
                try:
                    catalogs[toolset.id] = await self.available_presets(toolset.id)
                except (CMakeError, ToolchainError, ProcessError) as error:
                    catalogs[toolset.id] = CMakeError(str(error))
        return definitions, catalogs

    def resolve_profile(
        self,
        definition: ProfileDefinition,
        catalogs: dict[str, cmake.PresetsResult | CMakeError],
    ) -> CMakeProfile:
        """Validate selected presets and produce a resolved manual or preset profile."""
        if not definition.uses_presets:
            return self.plain_profile(definition)
        toolset = self.toolset(definition.toolset)
        available = catalogs[toolset.id]
        if isinstance(available, CMakeError):
            raise available
        validate_presets(definition, available, toolset.id)
        if definition.build_directory is not None:
            self.build_path(definition.build_directory)
        return CMakeProfile(
            name=definition.name,
            toolset_id=toolset.id,
            mode="presets",
            build_directory=definition.build_directory,
            configure_presets=definition.configure_presets or [],
            build_presets=definition.build_presets or [],
            test_presets=definition.test_presets or [],
            configuration=definition.configuration,
        )

    def operation_presets(self, profile: CMakeProfile, operation: str) -> list[str | None]:
        """Choose native presets or a permitted configured build-directory operation."""
        return operation_presets(profile, operation)

    def plain_profile(self, definition: ProfileDefinition) -> CMakeProfile:
        """Resolve a manual profile with a safe build directory and default Ninja generator."""
        return plain_profile(
            definition,
            self.toolset(definition.toolset).id,
            self.project_root,
            self.storage_root,
            self.protected_paths,
        )

    def build_path(self, directory: WorkspacePath) -> Path:
        """Resolve a checked build path using this service's managed roots."""
        return build_path(
            directory,
            self.project_root,
            self.storage_root,
            self.protected_paths,
        )

    def cache(self, directory: WorkspacePath) -> dict[str, str]:
        """Read a checked build cache using this service's managed roots."""
        return read_cache(
            directory,
            self.project_root,
            self.storage_root,
            self.protected_paths,
        )

    def require_configured(self, profile: CMakeProfile) -> dict[str, str]:
        """Require a readable cache belonging to this workspace and profile."""
        return require_configured(
            profile,
            self.project_root,
            self.storage_root,
            self.protected_paths,
        )

    def configure_definitions(self, definition: ProfileDefinition) -> dict[str, str]:
        """Resolve operator cache settings using this service's toolchains and roots."""
        return configure_definitions(
            definition,
            self.toolset(definition.toolset),
            self.toolchains,
            self.project_root,
            self.storage_root,
        )

    def current_contexts(self) -> list[CompilationContext]:
        """Copy confirmed contexts whose databases still exist."""
        available = []
        for context in self.configurations.values():
            snapshot = self.inputs.get(context.build_directory)
            if snapshot is None or not snapshot.current:
                continue
            try:
                database = compilation_database(
                    context.build_directory,
                    self.build_path(context.build_directory),
                )
            except (CMakeError, OSError):
                continue
            if database == context.compilation_database:
                available.append(context.model_copy(deep=True))
        return available

    async def compilation_contexts(
        self,
        profiles: Sequence[str] = (),
    ) -> list[CompilationContext]:
        """Return confirmed contexts after any in-flight controlled mutation finishes."""
        async with self.operation_lock:
            current = self.current_contexts()
        if profiles:
            definitions, _ = await self.selection(list(profiles))
            selected = {quote(definition.name, safe="") for definition in definitions}
            current = [item for item in current if item.id.split("/", 1)[0] in selected]
        return current

    async def configuration_updates(self) -> AsyncGenerator[list[CompilationContext], None]:
        """Yield independent current snapshots; slow subscribers retain only the latest update."""
        queue: asyncio.Queue[list[CompilationContext]] = asyncio.Queue(maxsize=1)
        async with self.operation_lock:
            current = self.current_contexts()
            self.configuration_subscribers[queue] = tuple(
                item.model_dump_json() for item in current
            )
            queue.put_nowait(current)
        try:
            while True:
                yield await queue.get()
        finally:
            self.configuration_subscribers.pop(queue, None)

    async def publish_configurations(self, *, refresh: bool = False) -> None:
        """Enqueue snapshots without waiting for a subscriber or acquiring its locks."""
        current = self.current_contexts()
        signature = tuple(item.model_dump_json() for item in current)
        for queue, previous in tuple(self.configuration_subscribers.items()):
            if signature == previous and not refresh:
                continue
            if queue.full():
                queue.get_nowait()
            queue.put_nowait([item.model_copy(deep=True) for item in current])
            self.configuration_subscribers[queue] = signature

    async def begin_source_change(
        self,
        paths: Sequence[WorkspacePath] | None = None,
    ) -> WorkspaceChange | None:
        """Capture known dependencies under supplied paths, or both roots for a batch mutation."""
        if paths is None:
            paths = (WorkspacePath("project/"), WorkspacePath("storage/"))
        if any(path.area == "root" for path in paths):
            return None
        await self.operation_lock.acquire()
        affected: set[WorkspacePath] = set()
        try:
            affected = {
                dependency
                for snapshot in self.inputs.values()
                for dependency in snapshot.checksums
                if any(
                    dependency.area == path.area
                    and Path(dependency.relative).is_relative_to(Path(path.relative))
                    for path in paths
                )
            }
            return WorkspaceChange(
                inputs=input_checksums(affected, self.project_root, self.storage_root),
                kinds={
                    path: path_kind(input_path(path, self.project_root, self.storage_root))
                    for path in paths
                },
            )
        except BaseException:
            for snapshot in self.inputs.values():
                if snapshot.structural or affected & snapshot.checksums.keys():
                    snapshot.current = False
            self.operation_lock.release()
            await self.publish_configurations()
            raise

    async def finish_source_change(
        self,
        context: WorkspaceChange,
        *,
        structural: bool = False,
        uncertain: bool = False,
    ) -> None:
        """Verify batch input and structural changes, or withdraw uncertain contexts."""
        try:
            changed = {
                path for path, digest in context.inputs.items()
                if input_checksums((path,), self.project_root, self.storage_root)[path] != digest
            }
            structural = structural or any(
                path_kind(input_path(path, self.project_root, self.storage_root)) != kind
                for path, kind in context.kinds.items()
            )
            for snapshot in self.inputs.values():
                if (
                    uncertain
                    or changed.intersection(snapshot.checksums)
                    or (structural and snapshot.structural)
                ):
                    snapshot.current = False
                elif any(path in snapshot.checksums for path in context.inputs):
                    snapshot.verify(self.project_root, self.storage_root)
        except BaseException:
            for snapshot in self.inputs.values():
                if snapshot.structural or context.inputs.keys() & snapshot.checksums.keys():
                    snapshot.current = False
            raise
        finally:
            self.operation_lock.release()
            await self.publish_configurations()

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        """Register profile inspection, configure, build, and test tools with their widgets."""
        icon = self.ICON.icon
        changes_files = ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=False,
            open_world_hint=True,
        )

        @apps.tool(
            resource_uri=self.PROFILES_WIDGET.uri,
            icons=[icon],
            annotations=ToolAnnotations(read_only_hint=True),
        )
        async def cmake_profiles(ctx: Context) -> Annotated[CallToolResult, list[CMakeProfile]]:
            """List effective profiles and their toolsets, build settings, and preset selections."""
            report = progress(ctx, interval=self.progress_interval)
            await report(0, message="Reading CMake profiles")
            try:
                definitions, catalogs = await self.selection(None)
                profiles = []
                for definition in definitions:
                    profile = self.resolve_profile(definition, catalogs)
                    profiles.append(profile)
                    await report(len(profiles), message=f"Read {profile.name}")
                return tool_result(profiles, render_profiles(profiles))
            except (CMakeError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error

        @apps.tool(
            resource_uri=self.CONFIGURE_WIDGET.uri,
            icons=[icon],
            annotations=changes_files,
        )
        async def cmake_configure(
            ctx: Context,
            profiles: list[str] | None = None,
            timeout: ProcessTimeout = ProcessTimeout(total=600),
        ) -> Annotated[CallToolResult, list[CMakeConfigureResult]]:
            """Configure selected profiles; return build/database paths and per-execution errors.

            Changing a manual profile's generator removes and recreates its build directory.
            """
            report = progress(ctx, interval=self.progress_interval)
            await report(0, message="Preparing CMake configure")
            try:
                definitions, catalogs = await self.selection(profiles)
            except (CMakeError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            results: list[CMakeConfigureResult] = []
            configured = {}
            updates = 0

            async def report_status(message: str) -> None:
                """Advance configure progress with the current profile and preset prefix."""
                nonlocal updates
                updates += 1
                label = profile.name if preset is None else f"{profile.name}/{preset}"
                await report(updates, message=f"{label}: {message}")

            for definition in definitions:
                try:
                    profile = self.resolve_profile(definition, catalogs)
                    presets = self.operation_presets(profile, "configure")
                    methods = self.cmake_methods(profile.toolset_id)
                except (CMakeError, ToolchainError) as error:
                    identifier = quote(definition.name, safe="")
                    for key in tuple(self.configurations):
                        if key == identifier or key.startswith(identifier + "/"):
                            del self.configurations[key]
                    await self.publish_configurations()
                    results.append(
                        CMakeConfigureResult(
                            profile=definition.name,
                            error=str(error),
                        )
                    )
                    continue
                for preset in presets:
                    result = CMakeConfigureResult(
                        profile=profile.name,
                        preset=preset,
                        build_directory=profile.build_directory,
                    )
                    results.append(result)
                    await self.operation_lock.acquire()
                    previous = None
                    try:
                        context = profile.compilation_context
                        if context is not None:
                            self.configurations[context.id] = context
                        key = (profile.toolset_id, preset, profile.build_directory)
                        if profile.build_directory is not None and key not in configured:
                            self.inputs[profile.build_directory].current = False
                            previous = generation(self.build_path(profile.build_directory))
                        if preset is not None and key in configured:
                            prior = configured[key]
                            result.error = prior.error
                            result.process_id = prior.process_id
                            result.compilation_database = prior.compilation_database
                            continue
                        build_directory = (
                            self.build_path(profile.build_directory)
                            if profile.build_directory is not None else None
                        )
                        settings = {}
                        if preset is None:
                            if profile.build_directory is None:
                                raise CMakeError("Plain configure requires a build directory.")
                            settings = self.configure_definitions(definition)
                            build_directory = self.build_path(profile.build_directory)
                            if (build_directory / "CMakeCache.txt").exists():
                                cache = self.require_configured(profile)
                                if cache.get("CMAKE_GENERATOR") != profile.generator:
                                    await report_status(
                                        "Clearing build directory for generator change"
                                    )
                                    clear_build_directory(
                                        build_directory,
                                        self.project_root
                                        if profile.build_directory.area == "project"
                                        else self.storage_root,
                                        self.storage_root,
                                        self.protected_paths,
                                    )
                                    await self.publish_configurations()
                        if profile.build_directory is not None:
                            prepare_queries(
                                self.build_path(
                                    WorkspacePath(
                                        f"{profile.build_directory}/.cmake/api/v1/query/"
                                        "client-forgemcp"
                                    )
                                )
                            )
                        await report_status("Starting configure")
                        command = await methods["configure"](
                            self.project_root,
                            build_directory,
                            preset=preset,
                            generator=profile.generator,
                            definitions=settings,
                            timeout=timeout,
                            on_progress=report_status,
                        )
                        result.process_id = command.process_id
                        if command.return_code != 0:
                            result.error = f"CMake configure exited with {command.return_code}."
                        if command.return_code == 0 and result.build_directory is not None:
                            snapshot = read_generation(
                                self.build_path(result.build_directory),
                                previous,
                                self.project_root,
                                self.storage_root,
                            )
                            if snapshot is None:
                                raise CMakeError(
                                    "Configure exited successfully but produced no new successful "
                                    "CMake File API generation in "
                                    f"{display_value(str(result.build_directory))}. "
                                    "The compilation context remains unconfirmed. Inspect "
                                    f"process_get(process_id={result.process_id}) and the "
                                    "build directory's .cmake/api/v1/reply metadata before "
                                    "retrying cmake_configure."
                                )
                            self.inputs[result.build_directory] = snapshot
                            result.compilation_database = compilation_database(
                                result.build_directory,
                                self.build_path(result.build_directory),
                            )
                        if preset is not None:
                            configured[key] = result
                    except (CMakeError, ToolchainError, ProcessError) as error:
                        result.error = str(error)
                        result.process_id = getattr(error, "process_id", result.process_id)
                    finally:
                        self.operation_lock.release()
                        await self.publish_configurations(refresh=result.error is None)
                    await report_status(result.error or "Completed successfully")
            return tool_result(results, render_executions(results, "Configure"))

        async def begin_build(profile: CMakeProfile) -> str | None:
            """Check known inputs and capture the index preceding this build command."""
            if profile.build_directory is None:
                return None
            self.inputs[profile.build_directory].verify(self.project_root, self.storage_root)
            await self.publish_configurations()
            return generation(self.build_path(profile.build_directory))

        async def finish_build(
            profile: CMakeProfile,
            previous: str | None,
            completed: bool,
            result: CMakeBuildResult,
        ) -> None:
            """Publish build generation and always release the command lock."""
            try:
                context = profile.compilation_context
                if context is not None:
                    self.configurations[context.id] = context
                if profile.build_directory is not None:
                    self.inputs[profile.build_directory].verify(
                        self.project_root,
                        self.storage_root,
                    )
                    if (
                        not completed
                        or generation(self.build_path(profile.build_directory)) is None
                    ):
                        self.inputs[profile.build_directory].current = False
                    if completed:
                        snapshot = read_generation(
                            self.build_path(profile.build_directory),
                            previous,
                            self.project_root,
                            self.storage_root,
                        )
                        if snapshot is not None:
                            self.inputs[profile.build_directory] = snapshot
            except (CMakeError, OSError) as error:
                if profile.build_directory is not None:
                    self.inputs[profile.build_directory].current = False
                result.error = result.error or str(error)
            finally:
                self.operation_lock.release()
                await self.publish_configurations(refresh=completed)

        @apps.tool(
            resource_uri=self.BUILD_WIDGET.uri,
            icons=[icon],
            annotations=changes_files,
        )
        async def cmake_build(
            ctx: Context,
            profiles: list[str] | None = None,
            targets: list[str] | None = None,
            parallel: Annotated[int | None, Field(ge=1)] = None,
            timeout: ProcessTimeout = ProcessTimeout(total=600),
        ) -> Annotated[CallToolResult, list[CMakeBuildResult]]:
            """Build selected profiles and their build presets after configuration.

            Omitted targets use CMake or preset defaults; a nonempty targets list
            selects explicit build targets.
            """
            report = progress(ctx, interval=self.progress_interval)
            await report(0, message="Preparing CMake build")
            try:
                validate_names(targets, "targets")
                definitions, catalogs = await self.selection(profiles)
            except (CMakeError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            results: list[CMakeBuildResult] = []
            updates = 0

            async def report_status(message: str) -> None:
                """Advance build progress with the current profile and preset prefix."""
                nonlocal updates
                updates += 1
                label = profile.name if preset is None else f"{profile.name}/{preset}"
                await report(updates, message=f"{label}: {message}")

            for definition in definitions:
                try:
                    profile = self.resolve_profile(definition, catalogs)
                    methods = self.cmake_methods(profile.toolset_id)
                    presets = self.operation_presets(profile, "build")
                except (CMakeError, ToolchainError) as error:
                    results.append(
                        CMakeBuildResult(
                            profile=definition.name,
                            error=str(error),
                        )
                    )
                    continue
                for preset in presets:
                    result = CMakeBuildResult(
                        profile=profile.name,
                        preset=preset,
                    )
                    results.append(result)
                    await self.operation_lock.acquire()
                    previous = None
                    completed = False
                    try:
                        previous = await begin_build(profile)
                        configuration = profile.configuration
                        result.configuration = configuration
                        build_directory = None
                        if preset is None:
                            cache = self.require_configured(profile)
                            require_configuration(cache, configuration)
                            build_directory = self.build_path(profile.build_directory)
                        await report_status("Starting build")
                        command = await methods["build"](
                            self.project_root,
                            build_directory,
                            preset=preset,
                            configuration=configuration,
                            targets=targets,
                            parallel=parallel,
                            timeout=timeout,
                            on_progress=report_status,
                        )
                        completed = True
                        result.completed_steps = command.completed_steps
                        result.total_steps = command.total_steps
                        result.process_id = command.process_id
                        if command.return_code != 0:
                            result.error = f"CMake build exited with {command.return_code}."
                    except (CMakeError, ToolchainError, ProcessError) as error:
                        result.error = str(error)
                        result.process_id = getattr(error, "process_id", result.process_id)
                    finally:
                        await finish_build(profile, previous, completed, result)
                    await report_status(result.error or "Completed successfully")
            return tool_result(results, render_executions(results, "Build"))

        @apps.tool(
            resource_uri=self.TEST_WIDGET.uri,
            icons=[icon],
            annotations=changes_files,
        )
        async def cmake_test(
            ctx: Context,
            profiles: list[str] | None = None,
            names: list[str] | None = None,
            parallel: Annotated[int | None, Field(ge=1)] = None,
            timeout: ProcessTimeout = ProcessTimeout(total=600),
        ) -> Annotated[CallToolResult, list[CMakeTestResult]]:
            """Run CTest for selected profiles and test presets after building; return test cases.

            names selects exact test names, not regex patterns; omit it to use
            CTest or preset defaults.
            """
            report = progress(ctx, interval=self.progress_interval)
            await report(0, message="Preparing CTest")
            try:
                validate_names(names, "names")
                definitions, catalogs = await self.selection(profiles)
            except (CMakeError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            results: list[CMakeTestResult] = []
            updates = 0

            async def report_status(message: str) -> None:
                """Advance test progress with the current profile and preset prefix."""
                nonlocal updates
                updates += 1
                label = profile.name if preset is None else f"{profile.name}/{preset}"
                await report(updates, message=f"{label}: {message}")

            for definition in definitions:
                try:
                    profile = self.resolve_profile(definition, catalogs)
                    methods = self.ctest_methods(profile.toolset_id)
                    presets = self.operation_presets(profile, "test")
                except (CMakeError, ToolchainError) as error:
                    results.append(
                        CMakeTestResult(
                            profile=definition.name,
                            error=str(error),
                        )
                    )
                    continue
                for preset in presets:
                    result = CMakeTestResult(
                        profile=profile.name,
                        preset=preset,
                    )
                    results.append(result)
                    try:
                        configuration = profile.configuration
                        result.configuration = configuration
                        build_directory = None
                        if preset is None:
                            cache = self.require_configured(profile)
                            require_configuration(cache, configuration)
                            build_directory = self.build_path(profile.build_directory)
                        temporary_root = self.build_path(WorkspacePath("storage/tmp"))
                        try:
                            temporary_root.mkdir(parents=True, exist_ok=True)
                        except OSError as error:
                            raise CMakeError(
                                "Cannot create CTest temporary storage storage/tmp: "
                                f"{os_error_reason(error)}."
                            ) from error
                        try:
                            with tempfile.TemporaryDirectory(
                                prefix="ctest-",
                                dir=temporary_root,
                            ) as temporary:
                                report_path = Path(temporary) / "results.xml"
                                await report_status("Starting tests")
                                command = await methods["test"](
                                    self.project_root,
                                    build_directory,
                                    report_path=report_path,
                                    preset=preset,
                                    configuration=configuration,
                                    names=names,
                                    parallel=parallel,
                                    timeout=timeout,
                                    on_progress=report_status,
                                )
                                result.process_id = command.process_id
                                result.tests = command.tests
                        except OSError as error:
                            raise CMakeError(
                                "Cannot use CTest temporary storage storage/tmp: "
                                f"{os_error_reason(error)}."
                            ) from error
                        failed = any(
                            test.status in ("failed", "not_run") for test in result.tests
                        )
                        if command.return_code != 0 or failed:
                            result.error = f"CTest failed (exit {command.return_code})."
                    except (CMakeError, ToolchainError, ProcessError) as error:
                        result.error = str(error)
                        result.process_id = getattr(error, "process_id", result.process_id)
                    await report_status(result.error or "Completed successfully")
            return tool_result(results, render_executions(results, "CTest"))

        @apps.tool(
            resource_uri=self.RUN_TEST_WIDGET.uri,
            icons=[icon],
            annotations=changes_files,
        )
        async def cmake_run_test(
            executable: WorkspacePath,
            ctx: Context,
            arguments: list[str] | None = None,
            cwd: WorkspacePath = WorkspacePath("project/"),
            timeout: ProcessTimeout = ProcessTimeout(total=600),
        ) -> Annotated[CallToolResult, CMakeRunTestResult]:
            """Run one project/storage test executable directly with literal command-line arguments.

            Inherits the server environment; cwd defaults to project/. Zero exit
            means success. No profiles, CTest properties, or case reports are used.
            Read output through process_get with the returned process_id.
            """
            report = progress(ctx, interval=self.progress_interval)
            await report(0, message="Preparing test executable")
            arguments = arguments if arguments is not None else []
            for index, value in enumerate(arguments):
                if "\0" in value:
                    raise ToolError(
                        f"arguments[{index}] contains NUL. Pass literal strings without NUL."
                    )
            try:
                program, directory = execution_paths(
                    executable,
                    cwd,
                    self.project_root,
                    self.storage_root,
                )
            except CMakeError as error:
                raise ToolError(str(error)) from error
            except OSError as error:
                raise ToolError(
                    f"Cannot inspect executable={display_value(str(executable))} or "
                    f"cwd={display_value(str(cwd))}: {os_error_reason(error)}."
                ) from error
            result = CMakeRunTestResult(
                executable=executable,
                arguments=arguments,
                cwd=cwd,
            )
            updates = 1
            await report(updates, message="Starting test executable")
            try:
                async with await self.processes.launch(
                    program,
                    arguments,
                    cwd=directory,
                    timeout=timeout,
                ) as session:
                    result.process_id = session.process_id
                    await session.close_stdin()
                    async for _ in session.output():
                        updates += 1
                        await report(updates, message="Receiving test output")
                    code = await session.wait()
                    if code != 0:
                        result.error = f"Test executable exited with {code}."
            except ProcessStartError as error:
                result.error = (
                    f"Cannot start executable={display_value(str(executable))} "
                    f"with cwd={display_value(str(cwd))}."
                    + (
                        f" {os_error_reason(error.__cause__)}."
                        if isinstance(error.__cause__, OSError)
                        else ""
                    )
                    + " Check that it is an executable program for this operating system "
                    "and that execution is permitted."
                )
            except ProcessError as error:
                result.error = str(error)
            await report(updates + 1, message=result.error or "Completed successfully")
            return tool_result(result, render_run_test(result))

        for widget in (
            self.PROFILES_WIDGET,
            self.CONFIGURE_WIDGET,
            self.BUILD_WIDGET,
            self.TEST_WIDGET,
            self.RUN_TEST_WIDGET,
        ):
            apps.add_html_resource(widget.uri, widget.content)
