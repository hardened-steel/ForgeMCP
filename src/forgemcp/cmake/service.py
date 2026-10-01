"""CMake profile selection, workspace integration, and MCP operations."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal, cast
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from forgemcp.assets import IconFile, Widget
from forgemcp.completion import Complete
from forgemcp.process.errors import ProcessError
from forgemcp.process.models import ProcessTimeout
from forgemcp.progress import progress
from forgemcp.toolchain.errors import ToolchainError
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.spec import Toolset
from forgemcp.toolchain.tools import cmake, ctest
from forgemcp.workspace.errors import WorkspaceError
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import WorkspaceService

from .errors import CMakeError
from .profiles import ProfileDefinition


class CMakeProfile(BaseModel):
    name: str
    toolset_id: str
    mode: Literal["plain", "presets"] = "plain"
    build_directory: WorkspacePath | None = None
    configure_presets: list[str] = Field(default_factory=list)
    build_presets: list[str] = Field(default_factory=list)
    test_presets: list[str] = Field(default_factory=list)
    configuration: str | None = None
    generator: str | None = None


class CompilationContext(BaseModel):
    id: str
    toolset_id: str
    compilation_database: WorkspacePath


class CMakeConfigureResult(BaseModel):
    profile: str
    preset: str | None = None
    build_directory: WorkspacePath | None = None
    compilation_database: WorkspacePath | None = None
    error: str | None = Field(
        default=None,
        description="Null on success; failure explanation otherwise.",
    )
    process_id: int | None = None


class CMakeBuildResult(BaseModel):
    profile: str
    preset: str | None = None
    configuration: str | None = None
    completed_steps: int | None = None
    total_steps: int | None = None
    error: str | None = Field(
        default=None,
        description="Null on success; failure explanation otherwise.",
    )
    process_id: int | None = None


class CMakeTestResult(BaseModel):
    profile: str
    preset: str | None = None
    configuration: str | None = None
    tests: list[ctest.TestCase] = Field(default_factory=list)
    error: str | None = Field(
        default=None,
        description="Null on success; failure explanation otherwise.",
    )
    process_id: int | None = None


class CMakeService:
    """Configure, build and test the CMake profiles selected by the operator.

    With no profile filter, commands run all profiles. Use a subset to diagnose a
    failure, then verify all profiles. Configure before building; build before
    testing. Compiler and preset choices belong to the operator, not tool arguments.
    """

    ICON = IconFile("icons/cmake.svg")
    PROFILES_WIDGET = Widget("assets/cmake-profiles.html")
    CONFIGURE_WIDGET = Widget("assets/cmake-configure.html")
    BUILD_WIDGET = Widget("assets/cmake-build.html")
    TEST_WIDGET = Widget("assets/cmake-test.html")

    def __init__(
        self,
        workspace: WorkspaceService,
        toolchains: ToolchainService,
        profiles: tuple[ProfileDefinition, ...] = (),
        *,
        project_root: Path,
        storage_root: Path,
        default_toolset: str = "system",
        progress_interval: float = 1.0,
    ) -> None:
        self.workspace = workspace
        self.project_root = project_root.resolve()
        self.storage_root = storage_root.resolve()
        self.toolchains = toolchains
        self.definitions = profiles
        self.default_toolset = default_toolset
        self.progress_interval = progress_interval

    def toolset(self, selector: str) -> Toolset:
        matches = [
            item
            for item in self.toolchains.list_toolsets()
            if selector in (item.id, item.name)
        ]
        if len(matches) != 1:
            raise CMakeError(f"Expected one toolset matching {selector!r}.")
        return matches[0]

    def cmake_methods(self, toolset_id: str) -> cmake.Methods:
        tool = self.toolchains.get_tool(toolset_id, "cmake")
        if tool is None:
            raise CMakeError(f"Toolset {toolset_id!r} has no cmake executable.")
        return cast(cmake.Methods, tool.methods)

    def ctest_methods(self, toolset_id: str) -> ctest.Methods:
        tool = self.toolchains.get_tool(toolset_id, "ctest")
        if tool is None:
            raise CMakeError(f"Toolset {toolset_id!r} has no ctest executable.")
        return cast(ctest.Methods, tool.methods)

    async def available_presets(self, toolset_id: str) -> cmake.PresetsResult:
        methods = self.cmake_methods(toolset_id)
        return await methods["presets"](self.project_root)

    async def selection(
        self,
        names: list[str] | None,
    ) -> tuple[list[ProfileDefinition], dict[str, cmake.PresetsResult | CMakeError]]:
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
            if not names or len(names) != len(set(names)):
                raise CMakeError("Profiles must be a nonempty list without duplicates.")
            unknown = set(names) - {definition.name for definition in definitions}
            if unknown:
                raise CMakeError(f"Unknown CMake profiles: {', '.join(sorted(unknown))}.")
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
        if not definition.uses_presets:
            return self.plain_profile(definition)
        toolset = self.toolset(definition.toolset)
        available = catalogs[toolset.id]
        if isinstance(available, CMakeError):
            raise available
        for kind in ("configure", "build", "test"):
            chosen = getattr(definition, kind + "_presets") or []
            if len(chosen) != len(set(chosen)):
                raise CMakeError(f"Repeated {kind} preset in profile {definition.name!r}.")
            unknown = set(chosen) - set(getattr(available, kind))
            if unknown:
                raise CMakeError(
                    f"Unavailable {kind} presets for {toolset.name}: {', '.join(sorted(unknown))}."
                )
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
        if profile.mode == "plain":
            return [None]
        selected = getattr(profile, operation + "_presets")
        if selected:
            return selected
        if operation != "configure" and profile.build_directory is not None:
            return [None]
        raise CMakeError(
            f"Profile {profile.name!r} has no {operation} presets. "
            "Select the required presets in --cmake-profile; for build/test without "
            "presets, set build-directory to the existing preset build directory."
        )

    def plain_profile(self, definition: ProfileDefinition) -> CMakeProfile:
        toolset = self.toolset(definition.toolset)
        directory = definition.build_directory
        if directory is None:
            if (
                not definition.name
                or definition.name in (".", "..")
                or any(char in definition.name for char in "/\\\0")
            ):
                raise CMakeError("Directory key must be one nonempty directory name.")
            directory = WorkspacePath(f"storage/build/cmake-{definition.name}")
        self.build_path(directory)
        return CMakeProfile(
            name=definition.name,
            toolset_id=toolset.id,
            build_directory=directory,
            configuration=definition.configuration,
            generator=definition.generator or "Ninja",
        )

    def build_path(self, directory: WorkspacePath) -> Path:
        path = self.workspace.writable_path(directory.relative, root=directory.area)
        if path == self.project_root:
            raise CMakeError("In-source CMake builds are not supported.")
        return path

    def cache(self, directory: WorkspacePath) -> dict[str, str]:
        path = self.build_path(directory) / "CMakeCache.txt"
        reference = WorkspacePath(f"{directory}/CMakeCache.txt")
        text = self.workspace.read_file(reference).text
        return cmake.parse_cache(text)

    def require_configured(self, profile: CMakeProfile) -> dict[str, str]:
        if profile.build_directory is None:
            raise CMakeError("Set build-directory for operations without a preset.")
        try:
            cache = self.cache(profile.build_directory)
        except WorkspaceError as error:
            raise CMakeError(f"Configure profile {profile.name!r} first.") from error
        source = cache.get("CMAKE_HOME_DIRECTORY")
        if source is None or Path(source).resolve() != self.project_root:
            raise CMakeError("Build directory belongs to another source project.")
        return cache

    def require_configuration(
        self,
        cache: dict[str, str],
        configuration: str | None,
    ) -> None:
        choices = cache.get("CMAKE_CONFIGURATION_TYPES", "").split(";")
        if choices != [""] and configuration not in choices:
            raise CMakeError(
                "Select an available multi-config configuration in the operator "
                f"profile or preset: {', '.join(choices)}."
            )
        if (
            choices == [""]
            and configuration is not None
            and cache.get("CMAKE_BUILD_TYPE") != configuration
        ):
            raise CMakeError(
                "The build directory has a different configuration; configure this profile first."
            )

    def configure_definitions(self, definition: ProfileDefinition) -> dict[str, str]:
        result = dict(definition.definitions)
        reserved = {"CMAKE_HOME_DIRECTORY", "CMAKE_CACHEFILE_DIR"}
        if reserved.intersection(key.partition(":")[0] for key in result):
            raise CMakeError("CMake source/build directories cannot be cache overrides.")
        toolset = self.toolset(definition.toolset)
        for field, variable in (
            (definition.c_compiler, "CMAKE_C_COMPILER"),
            (definition.cxx_compiler, "CMAKE_CXX_COMPILER"),
        ):
            if field is not None:
                tool = self.toolchains.get_tool(toolset.id, field)
                if tool is None:
                    raise CMakeError(f"Toolset {toolset.name!r} has no {field!r}.")
                if any(key.partition(":")[0] == variable for key in result):
                    raise CMakeError(f"Duplicate setting for {variable}.")
                result[variable] = str(tool.path)
        if definition.toolchain_file is not None:
            if any(key.partition(":")[0] == "CMAKE_TOOLCHAIN_FILE" for key in result):
                raise CMakeError("Duplicate setting for CMAKE_TOOLCHAIN_FILE.")
            reference = definition.toolchain_file
            if reference.area == "root":
                raise CMakeError("Toolchain file must be inside the project or storage root.")
            base = self.project_root if reference.area == "project" else self.storage_root
            toolchain_file = (base / reference.relative).resolve()
            if not toolchain_file.is_relative_to(base):
                raise CMakeError(f"Toolchain file is outside its root: {reference}.")
            if not toolchain_file.is_file():
                raise CMakeError(f"Toolchain file does not exist: {reference}.")
            result["CMAKE_TOOLCHAIN_FILE"] = str(toolchain_file)
        if definition.configuration is not None:
            configured = [
                value
                for key, value in result.items()
                if key.partition(":")[0] == "CMAKE_BUILD_TYPE"
            ]
            if configured and any(value != definition.configuration for value in configured):
                raise CMakeError("configuration conflicts with CMAKE_BUILD_TYPE.")
            if not configured:
                result["CMAKE_BUILD_TYPE"] = definition.configuration
        if not any(key.partition(":")[0] == "CMAKE_EXPORT_COMPILE_COMMANDS" for key in result):
            result["CMAKE_EXPORT_COMPILE_COMMANDS"] = "ON"
        if (definition.generator or "Ninja") in ("Ninja", "Ninja Multi-Config"):
            ninja = self.toolchains.get_tool(toolset.id, "ninja")
            if ninja is None:
                raise CMakeError(f"Toolset {toolset.name!r} has no ninja executable.")
            if not any(key.partition(":")[0] == "CMAKE_MAKE_PROGRAM" for key in result):
                result["CMAKE_MAKE_PROGRAM"] = str(ninja.path)
        return result

    def prepare_file_api(self, directory: WorkspacePath) -> None:
        self.workspace.mkdir(directory)
        reference = WorkspacePath(f"{directory}/.cmake/api/v1/query/client-forgemcp")
        self.workspace.mkdir(reference)
        for name in ("codemodel-v2", "cache-v2", "toolchains-v1"):
            target = WorkspacePath(f"{reference}/{name}")
            self.workspace.write_file(target, "")

    def compilation_database(self, directory: WorkspacePath) -> WorkspacePath | None:
        path = self.build_path(directory) / "compile_commands.json"
        return WorkspacePath(f"{directory}/compile_commands.json") if path.is_file() else None

    async def compilation_contexts(
        self,
        profiles: Sequence[str] = (),
    ) -> list[CompilationContext]:
        """Return configured contexts with existing databases; never configure here."""
        definitions, catalogs = await self.selection(list(profiles) if profiles else None)
        contexts = []
        for definition in definitions:
            try:
                profile = self.resolve_profile(definition, catalogs)
                presets = self.operation_presets(profile, "configure")
            except (CMakeError, WorkspaceError, ToolchainError):
                continue
            for preset in presets:
                directory = profile.build_directory
                if preset is not None and len(presets) != 1:
                    continue
                if directory is None:
                    continue
                try:
                    database = self.compilation_database(directory)
                    if database is None:
                        continue
                except (CMakeError, WorkspaceError, OSError):
                    continue
                identifier = quote(profile.name, safe="")
                if preset is not None:
                    identifier += "/" + quote(preset, safe="")
                contexts.append(
                    CompilationContext(
                        id=identifier,
                        toolset_id=profile.toolset_id,
                        compilation_database=database,
                    ),
                )
        return contexts

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
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
        async def cmake_profiles(ctx: Context) -> list[CMakeProfile]:
            """List operator profiles and their configure/build/test presets."""
            report = progress(ctx, interval=self.progress_interval)
            await report(0, message="Reading CMake profiles")
            try:
                definitions, catalogs = await self.selection(None)
                profiles = []
                for definition in definitions:
                    profile = self.resolve_profile(definition, catalogs)
                    profiles.append(profile)
                    await report(len(profiles), message=f"Read {profile.name}")
                return profiles
            except (CMakeError, WorkspaceError, ToolchainError, ProcessError) as error:
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
        ) -> list[CMakeConfigureResult]:
            """Configure all operator profiles, or an explicitly selected subset."""
            report = progress(ctx, interval=self.progress_interval)
            await report(0, message="Preparing CMake configure")
            try:
                definitions, catalogs = await self.selection(profiles)
            except (CMakeError, WorkspaceError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            results: list[CMakeConfigureResult] = []
            configured = {}
            updates = 0

            async def report_status(message: str) -> None:
                nonlocal updates
                updates += 1
                label = profile.name if preset is None else f"{profile.name}/{preset}"
                await report(updates, message=f"{label}: {message}")

            for definition in definitions:
                try:
                    profile = self.resolve_profile(definition, catalogs)
                    presets = self.operation_presets(profile, "configure")
                    methods = self.cmake_methods(profile.toolset_id)
                except (CMakeError, WorkspaceError, ToolchainError) as error:
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
                    try:
                        key = (profile.toolset_id, preset, profile.build_directory)
                        if preset is not None and key in configured:
                            prior = configured[key]
                            result.error = prior.error
                            result.process_id = prior.process_id
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
                                    await report_status("Clearing build directory for generator change")
                                    self.workspace.remove_directory(profile.build_directory)
                            self.prepare_file_api(profile.build_directory)
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
                            result.compilation_database = self.compilation_database(result.build_directory)
                        if preset is not None:
                            configured[key] = result
                    except (CMakeError, WorkspaceError, ToolchainError, ProcessError) as error:
                        result.error = str(error)
                        result.process_id = getattr(error, "process_id", result.process_id)
                    await report_status(result.error or "Completed successfully")
            return results

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
        ) -> list[CMakeBuildResult]:
            """Build all selected profiles and their build presets after configuration."""
            if targets is not None and (not targets or any(not name for name in targets)):
                raise ToolError("Targets must be a nonempty list of nonempty names.")
            report = progress(ctx, interval=self.progress_interval)
            await report(0, message="Preparing CMake build")
            try:
                definitions, catalogs = await self.selection(profiles)
            except (CMakeError, WorkspaceError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            results: list[CMakeBuildResult] = []
            updates = 0

            async def report_status(message: str) -> None:
                nonlocal updates
                updates += 1
                label = profile.name if preset is None else f"{profile.name}/{preset}"
                await report(updates, message=f"{label}: {message}")

            for definition in definitions:
                try:
                    profile = self.resolve_profile(definition, catalogs)
                    methods = self.cmake_methods(profile.toolset_id)
                    presets = self.operation_presets(profile, "build")
                except (CMakeError, WorkspaceError, ToolchainError) as error:
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
                    try:
                        configuration = profile.configuration
                        result.configuration = configuration
                        build_directory = None
                        if preset is None:
                            cache = self.require_configured(profile)
                            self.require_configuration(cache, configuration)
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
                        result.completed_steps = command.completed_steps
                        result.total_steps = command.total_steps
                        result.process_id = command.process_id
                        if command.return_code != 0:
                            result.error = f"CMake build exited with {command.return_code}."
                    except (CMakeError, WorkspaceError, ToolchainError, ProcessError) as error:
                        result.error = str(error)
                        result.process_id = getattr(error, "process_id", result.process_id)
                    await report_status(result.error or "Completed successfully")
            return results

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
        ) -> list[CMakeTestResult]:
            """Run CTest for selected profiles and their test presets after building."""
            if names is not None and (not names or any(not name for name in names)):
                raise ToolError("Test names must be a nonempty list of nonempty names.")
            report = progress(ctx, interval=self.progress_interval)
            await report(0, message="Preparing CTest")
            try:
                definitions, catalogs = await self.selection(profiles)
            except (CMakeError, WorkspaceError, ToolchainError, ProcessError) as error:
                raise ToolError(str(error)) from error
            results: list[CMakeTestResult] = []
            updates = 0

            async def report_status(message: str) -> None:
                nonlocal updates
                updates += 1
                label = profile.name if preset is None else f"{profile.name}/{preset}"
                await report(updates, message=f"{label}: {message}")

            for definition in definitions:
                try:
                    profile = self.resolve_profile(definition, catalogs)
                    methods = self.ctest_methods(profile.toolset_id)
                    presets = self.operation_presets(profile, "test")
                except (CMakeError, WorkspaceError, ToolchainError) as error:
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
                            self.require_configuration(cache, configuration)
                            build_directory = self.build_path(profile.build_directory)
                        with self.workspace.temporary_directory("ctest") as temporary:
                            report_path = temporary.path / "results.xml"
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
                        failed = any(
                            test.status in ("failed", "not_run") for test in result.tests
                        )
                        if command.return_code != 0 or failed:
                            result.error = f"CTest failed (exit {command.return_code})."
                    except (CMakeError, WorkspaceError, ToolchainError, ProcessError) as error:
                        result.error = str(error)
                        result.process_id = getattr(error, "process_id", result.process_id)
                    await report_status(result.error or "Completed successfully")
            return results

        for widget in (
            self.PROFILES_WIDGET,
            self.CONFIGURE_WIDGET,
            self.BUILD_WIDGET,
            self.TEST_WIDGET,
        ):
            apps.add_html_resource(widget.uri, widget.content)
