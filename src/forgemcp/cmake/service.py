"""Transport-neutral CMake and CTest orchestration through ForgeMCP services."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol, TypeAlias

from forgemcp.cmake.errors import (
    CMakeFileApiError,
    CMakePresetError,
    CMakeRequestError,
    CMakeToolUnavailableError,
    CTestJsonError,
)
from forgemcp.cmake.models import (
    CMakeBuildPreset,
    CMakeBuildResult,
    CMakeConfigurationTargets,
    CMakeConfigurePreset,
    CMakeConfigureResult,
    CMakePresetList,
    CMakeResolvedProfile,
    CMakeStatus,
    CMakeTargetList,
    CMakeTargetMetadata,
    CMakeTestPreset,
    CMakeToolStatus,
    CMakeVersion,
    CTestRunResult,
    CTestTest,
    CTestTestList,
)
from forgemcp.core.config import ConfigurationSource, ForgeConfig
from forgemcp.models import ProcessResult
from forgemcp.processes import ProcessError
from forgemcp.workspace import (
    GeneratedWorkspaceDirectory,
    WorkspaceError,
    WorkspaceFileNotFoundError,
    WorkspaceService,
)
from forgemcp.toolchain import ToolchainDiscoveryService


MINIMUM_CMAKE_VERSION = CMakeVersion(major=3, minor=23, patch=0, full="3.23.0")
"""First CMake release supported by the preset, File API, and CTest slice."""

MAX_PARALLEL_JOBS = 256
"""Largest explicit parallel build value accepted by the public API."""

_VERSION_BANNER = re.compile(r"\b(?:cmake|ctest)\s+version\s+(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)
_CACHE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FAILED_TEST_LINE = re.compile(r"^\s*\d+\s*-\s*(?P<name>.+?)\s+\([^)]*\)\s*$")

CacheValue: TypeAlias = str | int | bool


@dataclass(frozen=True, slots=True)
class CMakeOperationStatusCache:
    """Safe content-free metadata retained after one CMake/CTest operation."""

    operation: str
    outcome: str
    binary_dir: str
    exit_code: int | None
    duration_milliseconds: int
    item_count: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class CMakeProjectStatusCache:
    """Only already-observed CMake state used by project status."""

    tool_status: CMakeStatus | None
    configured_binary_dir: str | None
    active_operations: int
    last_configure: CMakeOperationStatusCache | None
    last_build: CMakeOperationStatusCache | None
    last_test: CMakeOperationStatusCache | None


class ProcessRunner(Protocol):
    """The short-command portion of ProcessRuntime required by CMakeService."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        environment: Mapping[str, str] | None = None,
        inherit_environment: bool | None = None,
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        """Run a bounded argv command and return its structured process result."""


class CMakeService:
    """Safe, application-scoped CMake configure, File API, build, and CTest service.

    All executable invocations go through the supplied :class:`ProcessRunner`.
    The Workspace service owns every generated build-tree read and write, so
    CMake-reported paths never become trusted ``Path`` values in this module.
    """

    def __init__(
        self,
        workspace: WorkspaceService,
        process_runtime: ProcessRunner,
        config: ForgeConfig | None = None,
        toolchain: ToolchainDiscoveryService | None = None,
    ) -> None:
        self._workspace = workspace
        self._process_runtime = process_runtime
        self._config = config
        self._toolchain = toolchain
        self._cached_tool_status: CMakeStatus | None = None
        self._configured_binary_dir: str | None = None
        self._active_operations = 0
        self._last_configure: CMakeOperationStatusCache | None = None
        self._last_build: CMakeOperationStatusCache | None = None
        self._last_test: CMakeOperationStatusCache | None = None
        self._resolved_profile: CMakeResolvedProfile | None = None

    def cached_project_status(self) -> CMakeProjectStatusCache:
        """Return content-free metadata without filesystem access or tool probes."""

        return CMakeProjectStatusCache(
            tool_status=self._cached_tool_status,
            configured_binary_dir=self._configured_binary_dir,
            active_operations=self._active_operations,
            last_configure=self._last_configure,
            last_build=self._last_build,
            last_test=self._last_test,
        )

    async def status(self) -> CMakeStatus:
        """Discover CMake and CTest and report their parseable, supported versions."""
        cmake = await self._tool_status("cmake", requires_minimum=True)
        ctest = await self._tool_status("ctest", requires_minimum=False)
        profile = self._resolve_profile(binary_dir=None, source_dir=None, preset=None)
        self._resolved_profile = profile
        status = CMakeStatus(
            available=cmake.available and cmake.supported and ctest.available,
            minimum_cmake_version=MINIMUM_CMAKE_VERSION,
            cmake=cmake,
            ctest=ctest,
            profile=profile,
        )
        self._cached_tool_status = status
        return status

    async def list_presets(self, *, source_dir: str | None = None) -> CMakePresetList:
        """Return safe preset summaries without evaluating CMake inheritance or macros."""
        source = self._workspace.require_directory(
            source_dir if source_dir is not None else self._config.cmake_source_dir if self._config is not None else "."
        )
        preset_files: list[str] = []
        configure: list[CMakeConfigurePreset] = []
        build: list[CMakeBuildPreset] = []
        test: list[CMakeTestPreset] = []
        for filename in ("CMakePresets.json", "CMakeUserPresets.json"):
            path = filename if source == "." else f"{source}/{filename}"
            try:
                text = self._workspace.read_text(path)[0]
            except WorkspaceFileNotFoundError:
                continue
            document = self._parse_preset_document(text, path)
            preset_files.append(path)
            configure.extend(self._configure_presets(document, path))
            build.extend(self._build_presets(document, path))
            test.extend(self._test_presets(document, path))
        return CMakePresetList(
            source_dir=source,
            preset_files=tuple(preset_files),
            configure_presets=tuple(configure),
            build_presets=tuple(build),
            test_presets=tuple(test),
        )

    async def configure(
        self,
        *,
        source_dir: str | None = None,
        binary_dir: str | None = None,
        preset: str | None = None,
        cache_variables: Mapping[str, CacheValue] | None = None,
    ) -> CMakeConfigureResult:
        """Configure a safe generated build directory using a preset or direct mode."""
        started = monotonic()
        self._active_operations += 1
        normalised_binary = "."
        try:
            profile = self._resolve_profile(binary_dir=binary_dir, source_dir=source_dir, preset=preset)
            source = self._workspace.require_directory(profile.source_dir)
            generated = self._workspace.open_generated_directory(profile.binary_dir, create=True)
            normalised_binary = generated.relative_path
            if generated.relative_path == source:
                raise CMakeRequestError("CMake source_dir and binary_dir must be different directories.")
            preset_name = self._selected_preset(preset)
            generated.write_text(".cmake/api/v1/query/codemodel-v2", "")

            argv = [self._tool_executable("cmake"), "-S", source, "-B", generated.relative_path]
            if preset_name is not None:
                argv.extend(["--preset", preset_name])
            elif self._config is not None and self._config.cmake_generator is not None:
                argv.extend(["-G", self._config.cmake_generator])
                if (
                    self._config.target_arch != "auto"
                    and self._config.cmake_generator.casefold().startswith("visual studio")
                ):
                    argv.extend(["-A", self._config.target_arch])
            argv.extend(self._cache_arguments(cache_variables))
            result = await self._run_required("cmake", argv, timeout_seconds=self._default_timeout("configure"))
            response = CMakeConfigureResult(
                source_dir=source,
                binary_dir=generated.relative_path,
                preset=preset_name,
                process=result,
            )
        except asyncio.CancelledError:
            self._last_configure = self._operation_cache("configure", "cancelled", normalised_binary, started)
            raise
        except Exception:
            self._last_configure = self._operation_cache("configure", "failure", normalised_binary, started)
            raise
        else:
            outcome = "success" if result.exit_code == 0 and not result.timed_out else "failure"
            self._last_configure = self._operation_cache(
                "configure", outcome, generated.relative_path, started, exit_code=result.exit_code
            )
            if outcome == "success":
                self._configured_binary_dir = generated.relative_path
                self._resolved_profile = CMakeResolvedProfile(
                    source_dir=source,
                    binary_dir=generated.relative_path,
                    source_dir_source=profile.source_dir_source,
                    binary_dir_source=profile.binary_dir_source,
                    configure_preset_source=profile.configure_preset_source,
                )
            return response
        finally:
            self._active_operations -= 1

    def list_targets(self, *, binary_dir: str | None = None) -> CMakeTargetList:
        """Read target metadata solely from CMake File API codemodel v2."""
        generated = self._workspace.open_generated_directory(self._resolve_profile(binary_dir=binary_dir, source_dir=None, preset=None).binary_dir)
        codemodel = self._load_codemodel(generated)
        configurations = self._parse_target_configurations(codemodel, generated)
        return CMakeTargetList(binary_dir=generated.relative_path, configurations=configurations)

    async def build(
        self,
        *,
        binary_dir: str | None = None,
        targets: Iterable[str] = (),
        configuration: str | None = None,
        parallel_jobs: int | None = None,
    ) -> CMakeBuildResult:
        """Build the default project or explicit target names without invoking a shell."""
        started = monotonic()
        self._active_operations += 1
        normalised_binary = "."
        target_names: tuple[str, ...] = ()
        try:
            profile = self._resolve_profile(binary_dir=binary_dir, source_dir=None, preset=None)
            generated = self._workspace.open_generated_directory(profile.binary_dir)
            normalised_binary = generated.relative_path
            target_names = self._validate_names(targets, label="target")
            selected_configuration = self._selected_configuration(configuration)
            jobs = self._validate_parallel_jobs(parallel_jobs)
            argv = [self._tool_executable("cmake"), "--build", generated.relative_path]
            if target_names:
                argv.extend(["--target", *target_names])
            if selected_configuration is not None:
                argv.extend(["--config", selected_configuration])
            if jobs is not None:
                argv.extend(["--parallel", str(jobs)])
            result = await self._run_required("cmake", argv, timeout_seconds=self._default_timeout("build"))
            response = CMakeBuildResult(
                binary_dir=generated.relative_path,
                targets=target_names,
                configuration=selected_configuration,
                process=result,
            )
        except asyncio.CancelledError:
            self._last_build = self._operation_cache("build", "cancelled", normalised_binary, started, item_count=len(target_names))
            raise
        except Exception:
            self._last_build = self._operation_cache("build", "failure", normalised_binary, started, item_count=len(target_names))
            raise
        else:
            outcome = "success" if result.exit_code == 0 and not result.timed_out else "failure"
            self._last_build = self._operation_cache("build", outcome, generated.relative_path, started, exit_code=result.exit_code, item_count=len(target_names))
            return response
        finally:
            self._active_operations -= 1

    async def list_tests(self, *, binary_dir: str | None = None) -> CTestTestList:
        """List tests through CTest's documented ``json-v1`` output format."""
        generated = self._workspace.open_generated_directory(self._resolve_profile(binary_dir=binary_dir, source_dir=None, preset=None).binary_dir)
        result = await self._run_required(
            "ctest", [self._tool_executable("ctest"), "--test-dir", generated.relative_path, "--show-only=json-v1"],
            timeout_seconds=self._default_timeout("test"),
        )
        if result.exit_code != 0 or result.timed_out:
            raise CTestJsonError("CTest could not produce a JSON test listing for this build directory.")
        tests = self._parse_ctest_json(result.stdout.text)
        return CTestTestList(binary_dir=generated.relative_path, tests=tests, process=result)

    async def run_tests(
        self,
        *,
        binary_dir: str | None = None,
        test_names: Iterable[str] = (),
        configuration: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CTestRunResult:
        """Run all tests or an exact-name subset, with ProcessRuntime limits in force."""
        started = monotonic()
        self._active_operations += 1
        normalised_binary = "."
        names: tuple[str, ...] = ()
        try:
            profile = self._resolve_profile(binary_dir=binary_dir, source_dir=None, preset=None)
            generated = self._workspace.open_generated_directory(profile.binary_dir)
            normalised_binary = generated.relative_path
            names = self._validate_names(test_names, label="test name")
            selected_configuration = self._selected_configuration(configuration)
            argv = [self._tool_executable("ctest"), "--test-dir", generated.relative_path, "--output-on-failure"]
            if selected_configuration is not None:
                argv.extend(["--build-config", selected_configuration])
            if names:
                argv.extend(["-R", "^(?:" + "|".join(re.escape(name) for name in names) + ")$"])
            result = await self._run_required(
                "ctest",
                argv,
                timeout_seconds=(
                    self._default_timeout("test")
                    if timeout_seconds is None
                    else timeout_seconds
                ),
            )
            failed_tests = self._failed_tests(result)
            response = CTestRunResult(
                binary_dir=generated.relative_path,
                test_names=names,
                configuration=selected_configuration,
                failed_tests=failed_tests,
                process=result,
            )
        except asyncio.CancelledError:
            self._last_test = self._operation_cache("test", "cancelled", normalised_binary, started, item_count=len(names))
            raise
        except Exception:
            self._last_test = self._operation_cache("test", "failure", normalised_binary, started, item_count=len(names))
            raise
        else:
            outcome = "success" if result.exit_code == 0 and not result.timed_out else "failure"
            self._last_test = self._operation_cache("test", outcome, generated.relative_path, started, exit_code=result.exit_code, item_count=len(names) if names else len(failed_tests))
            return response
        finally:
            self._active_operations -= 1

    @staticmethod
    def _operation_cache(
        operation: str,
        outcome: str,
        binary_dir: str,
        started: float,
        *,
        exit_code: int | None = None,
        item_count: int = 0,
    ) -> CMakeOperationStatusCache:
        return CMakeOperationStatusCache(
            operation=operation,
            outcome=outcome,
            binary_dir=binary_dir[:4096] or ".",
            exit_code=exit_code,
            duration_milliseconds=max(0, int((monotonic() - started) * 1000)),
            item_count=item_count,
            observed_at=datetime.now(UTC),
        )

    async def _tool_status(self, executable: str, *, requires_minimum: bool) -> CMakeToolStatus:
        """Convert absence, command failure, and banner parsing into safe status data."""
        try:
            result = await self._run_required(
                executable, [self._tool_executable(executable), "--version"]
            )
        except (CMakeToolUnavailableError, ProcessError):
            return CMakeToolStatus(
                executable=executable,
                available=False,
                supported=False,
                error=f"{executable} was not found or is not permitted by the process policy.",
            )
        if result.timed_out or result.exit_code != 0:
            return CMakeToolStatus(
                executable=executable,
                available=False,
                supported=False,
                error=f"{executable} could not report its version successfully.",
            )
        match = _VERSION_BANNER.search(result.stdout.text) or _VERSION_BANNER.search(result.stderr.text)
        if match is None:
            return CMakeToolStatus(
                executable=executable,
                available=False,
                supported=False,
                error=f"{executable} returned a version banner ForgeMCP could not parse.",
            )
        version = CMakeVersion(
            major=int(match.group(1)),
            minor=int(match.group(2)),
            patch=int(match.group(3) or "0"),
            full=match.group(0).rsplit(" ", 1)[-1],
        )
        supported = not requires_minimum or version.at_least(MINIMUM_CMAKE_VERSION)
        return CMakeToolStatus(
            executable=executable,
            available=True,
            version=version,
            supported=supported,
            error=(
                None
                if supported
                else f"cmake {version.full} is below the supported minimum {MINIMUM_CMAKE_VERSION.full}."
            ),
        )

    async def _run_required(
        self, executable: str, argv: Sequence[str], *, timeout_seconds: float | None = None
    ) -> ProcessResult:
        """Run a fixed executable and make runtime absence a safe CMake domain error."""
        try:
            runner = (
                getattr(self._process_runtime, "run_toolchain")
                if self._toolchain is not None and hasattr(self._process_runtime, "run_toolchain")
                else self._process_runtime.run
            )
            return await runner(argv, cwd=".", timeout_seconds=timeout_seconds)
        except ProcessError as error:
            raise CMakeToolUnavailableError(
                f"{executable} is not available through the configured Process Runtime."
            ) from error

    def _tool_executable(self, tool: str) -> str:
        """Return Discovery's exact approved path without exposing it in models."""
        if self._toolchain is not None:
            selected = self._toolchain.executable(tool)
            if selected is not None:
                return str(selected)
            raise CMakeToolUnavailableError(
                f"{tool} is unavailable from the configured toolchain discovery service."
            )
        return tool

    def _default_timeout(self, operation: str) -> float | None:
        if self._config is None:
            return None
        return {
            "configure": self._config.configure_timeout_seconds,
            "build": self._config.build_timeout_seconds,
            "test": self._config.test_timeout_seconds,
        }[operation]

    def _selected_preset(self, value: str | None) -> str | None:
        requested = self._validate_optional_name(value, label="preset")
        if requested is not None:
            return requested
        if self._config is None:
            return None
        return self._validate_optional_name(self._config.configure_preset, label="preset")

    def _selected_configuration(self, value: str | None) -> str | None:
        requested = self._validate_optional_name(value, label="configuration")
        if requested is not None:
            return requested
        if self._config is None:
            return None
        return self._validate_optional_name(self._config.default_configuration, label="configuration")

    def _resolve_profile(
        self, *, binary_dir: str | None, source_dir: str | None, preset: str | None
    ) -> CMakeResolvedProfile:
        """Resolve request → CLI/env config → preset → default safely.

        The actual workspace checks happen immediately afterwards through
        WorkspaceService, so a preset cannot cause an external build tree.
        """
        if source_dir is not None:
            source, source_origin = source_dir, "request"
        elif self._config is not None:
            source, source_origin = self._config.cmake_source_dir, self._config.source_of("cmake_source_dir").value
        else:
            source, source_origin = ".", ConfigurationSource.DEFAULT.value
        selected_preset = self._selected_preset(preset)
        if binary_dir is not None:
            binary, binary_origin = binary_dir, "request"
        elif self._config is not None and self._config.build_dir is not None:
            binary, binary_origin = self._config.build_dir, self._config.source_of("build_dir").value
        else:
            from_preset = self._preset_binary_dir(source, selected_preset)
            if from_preset is not None:
                binary, binary_origin = from_preset, ConfigurationSource.DISCOVERY.value
            else:
                binary, binary_origin = "build", ConfigurationSource.DEFAULT.value
        # Validate now, including every CLI/environment/preset candidate. It is
        # intentionally not returned as a host path.
        normal_source = self._workspace.require_directory(source)
        normal_binary = self._workspace.validate_generated_directory_path(binary)
        return CMakeResolvedProfile(
            source_dir=normal_source,
            binary_dir=normal_binary,
            source_dir_source=source_origin,
            binary_dir_source=binary_origin,
            configure_preset_source=("request" if preset is not None else self._config.source_of("configure_preset").value if self._config is not None and self._config.configure_preset is not None else ConfigurationSource.DEFAULT.value),
        )

    def _preset_binary_dir(self, source_dir: str, preset: str | None) -> str | None:
        if preset is None:
            return None
        source = self._workspace.require_directory(source_dir)
        for filename in ("CMakePresets.json", "CMakeUserPresets.json"):
            path = filename if source == "." else f"{source}/{filename}"
            try:
                text = self._workspace.read_text(path)[0]
            except WorkspaceFileNotFoundError:
                continue
            document = self._parse_preset_document(text, path)
            for item in self._preset_entries(document, "configurePresets"):
                if item.get("name") != preset:
                    continue
                inherits = item.get("inherits")
                if inherits not in (None, (), []):
                    raise CMakePresetError(
                        "The selected configure preset uses inheritance that ForgeMCP does not interpret; provide binary_dir explicitly."
                    )
                if item.get("condition") is not None:
                    raise CMakePresetError(
                        "The selected configure preset uses a condition that ForgeMCP does not interpret; provide binary_dir explicitly."
                    )
                value = item.get("binaryDir")
                if value is None:
                    raise CMakePresetError(
                        "The selected configure preset has no direct binaryDir; provide binary_dir explicitly."
                    )
                if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
                    raise CMakePresetError("The selected configure preset has an unsafe binaryDir.")
                # CMake owns general preset macro expansion.  The one static
                # macro required to derive a safe pre-configure default is
                # expanded locally; every other macro remains ambiguous and
                # is rejected rather than guessed.
                expanded = value.replace("${sourceDir}", source)
                if "$" in expanded:
                    raise CMakePresetError("The selected configure preset has an unsafe binaryDir.")
                return expanded
        return None

    @staticmethod
    def _parse_preset_document(text: str, path: str) -> Mapping[str, object]:
        try:
            document = json.loads(text)
        except json.JSONDecodeError as error:
            raise CMakePresetError("A CMake preset document is not valid JSON.") from error
        if not isinstance(document, dict):
            raise CMakePresetError("A CMake preset document must contain a JSON object.")
        if not isinstance(document.get("version"), int):
            raise CMakePresetError("A CMake preset document must declare an integer version.")
        return document

    def _configure_presets(self, document: Mapping[str, object], path: str) -> tuple[CMakeConfigurePreset, ...]:
        entries = self._preset_entries(document, "configurePresets")
        return tuple(
            CMakeConfigurePreset(
                name=self._preset_name(entry),
                source_file=path,
                display_name=self._optional_text(entry.get("displayName")),
                description=self._optional_text(entry.get("description")),
                hidden=self._optional_bool(entry.get("hidden")),
                generator=self._optional_text(entry.get("generator")),
            )
            for entry in entries
        )

    def _build_presets(self, document: Mapping[str, object], path: str) -> tuple[CMakeBuildPreset, ...]:
        entries = self._preset_entries(document, "buildPresets")
        return tuple(
            CMakeBuildPreset(
                name=self._preset_name(entry),
                source_file=path,
                display_name=self._optional_text(entry.get("displayName")),
                description=self._optional_text(entry.get("description")),
                hidden=self._optional_bool(entry.get("hidden")),
                configure_preset=self._optional_text(entry.get("configurePreset")),
                configuration=self._optional_text(entry.get("configuration")),
                targets=self._optional_text_array(entry.get("targets")),
            )
            for entry in entries
        )

    def _test_presets(self, document: Mapping[str, object], path: str) -> tuple[CMakeTestPreset, ...]:
        entries = self._preset_entries(document, "testPresets")
        return tuple(
            CMakeTestPreset(
                name=self._preset_name(entry),
                source_file=path,
                display_name=self._optional_text(entry.get("displayName")),
                description=self._optional_text(entry.get("description")),
                hidden=self._optional_bool(entry.get("hidden")),
                configure_preset=self._optional_text(entry.get("configurePreset")),
                configuration=self._optional_text(entry.get("configuration")),
            )
            for entry in entries
        )

    @staticmethod
    def _preset_entries(document: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
        value = document.get(key, [])
        if not isinstance(value, list) or any(not isinstance(entry, dict) for entry in value):
            raise CMakePresetError(f"Preset field '{key}' must be an array of objects.")
        return tuple(value)

    @staticmethod
    def _preset_name(entry: Mapping[str, object]) -> str:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise CMakePresetError("Every CMake preset must have a non-empty name.")
        return name

    @staticmethod
    def _optional_text(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise CMakePresetError("A published preset text field must be a string.")
        return value

    @staticmethod
    def _optional_bool(value: object) -> bool:
        if value is None:
            return False
        if not isinstance(value, bool):
            raise CMakePresetError("A published preset hidden field must be a boolean.")
        return value

    @staticmethod
    def _optional_text_array(value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            raise CMakePresetError("A published preset target list must contain non-empty strings.")
        return tuple(value)

    def _cache_arguments(self, cache_variables: Mapping[str, CacheValue] | None) -> tuple[str, ...]:
        if cache_variables is None:
            return ()
        if not isinstance(cache_variables, Mapping):
            raise CMakeRequestError("cache_variables must be a mapping of validated scalar values.")
        arguments: list[str] = []
        for name, value in cache_variables.items():
            if not isinstance(name, str) or not _CACHE_NAME.fullmatch(name):
                raise CMakeRequestError("Cache variable names must be CMake-style identifiers.")
            if isinstance(value, bool):
                rendered = "ON" if value else "OFF"
            elif isinstance(value, int):
                rendered = str(value)
            elif isinstance(value, str) and "\x00" not in value:
                rendered = value
            else:
                raise CMakeRequestError("Cache variable values must be NUL-free strings, integers, or booleans.")
            arguments.append(f"-D{name}:STRING={rendered}")
        return tuple(arguments)

    @staticmethod
    def _validate_optional_name(value: str | None, *, label: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or "\x00" in value or value.startswith("-"):
            raise CMakeRequestError(f"The {label} must be a non-empty NUL-free name that does not start with '-'.")
        return value

    def _validate_names(self, values: Iterable[str], *, label: str) -> tuple[str, ...]:
        if isinstance(values, str):
            raise CMakeRequestError(f"{label.capitalize()} values must be an array of exact names.")
        try:
            names = tuple(values)
        except TypeError as error:
            raise CMakeRequestError(f"{label.capitalize()} values must be an array of exact names.") from error
        if len(set(names)) != len(names):
            raise CMakeRequestError(f"{label.capitalize()} values must not contain duplicates.")
        return tuple(self._validate_optional_name(name, label=label) for name in names)  # type: ignore[arg-type]

    @staticmethod
    def _validate_parallel_jobs(value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_PARALLEL_JOBS:
            raise CMakeRequestError(f"parallel_jobs must be an integer from 1 through {MAX_PARALLEL_JOBS}.")
        return value

    def _load_codemodel(self, generated: GeneratedWorkspaceDirectory) -> Mapping[str, object]:
        reply_directory = ".cmake/api/v1/reply"
        try:
            names = generated.list_files(reply_directory)
        except WorkspaceError as error:
            raise CMakeFileApiError("CMake File API reply directory is missing; configure this build tree first.") from error
        indexes = [name for name in names if name.startswith("index-") and name.endswith(".json")]
        if not indexes:
            raise CMakeFileApiError("CMake File API reply is missing; configure this build tree after requesting codemodel-v2.")
        snapshots = [
            (generated.get_snapshot(f"{reply_directory}/{name}"), name)
            for name in indexes
        ]
        index_name = max(
            snapshots,
            key=lambda item: (item[0].modified_at or item[0].captured_at, item[1]),
        )[1]
        index_snapshot = next(snapshot for snapshot, name in snapshots if name == index_name)
        query_snapshot = generated.get_snapshot(".cmake/api/v1/query/codemodel-v2")
        if (
            query_snapshot.exists
            and query_snapshot.modified_at is not None
            and index_snapshot.modified_at is not None
            and index_snapshot.modified_at < query_snapshot.modified_at
        ):
            raise CMakeFileApiError(
                "CMake File API reply predates the current codemodel-v2 query; configure this build tree again."
            )
        index = self._read_file_api_json(generated, f"{reply_directory}/{index_name}")
        reply = index.get("reply")
        if not isinstance(reply, Mapping):
            raise CMakeFileApiError("CMake File API index reply is malformed or stale.")
        object_entry = reply.get("codemodel-v2")
        if not isinstance(object_entry, Mapping):
            raise CMakeFileApiError("CMake File API reply is stale or does not contain codemodel-v2.")
        json_file = object_entry.get("jsonFile")
        if not isinstance(json_file, str) or not json_file:
            raise CMakeFileApiError("CMake File API codemodel reply does not identify its JSON object.")
        codemodel = self._read_file_api_json(generated, f"{reply_directory}/{json_file}")
        version = codemodel.get("version")
        if not isinstance(version, Mapping) or version.get("major") != 2:
            raise CMakeFileApiError("CMake File API codemodel reply is not version 2.")
        if not isinstance(codemodel.get("configurations"), list):
            raise CMakeFileApiError("CMake File API codemodel reply has no configurations array.")
        return codemodel

    def _read_file_api_json(
        self, generated: GeneratedWorkspaceDirectory, path: str
    ) -> Mapping[str, object]:
        try:
            content = generated.read_text(path)
            value = json.loads(content)
        except (WorkspaceError, json.JSONDecodeError) as error:
            raise CMakeFileApiError("CMake File API contains a missing or malformed JSON reply.") from error
        if not isinstance(value, dict):
            raise CMakeFileApiError("CMake File API JSON replies must contain objects.")
        return value

    def _parse_target_configurations(
        self, codemodel: Mapping[str, object], generated: GeneratedWorkspaceDirectory
    ) -> tuple[CMakeConfigurationTargets, ...]:
        configurations = codemodel["configurations"]
        assert isinstance(configurations, list)  # Checked by _load_codemodel.
        paths = codemodel.get("paths")
        if not isinstance(paths, Mapping):
            raise CMakeFileApiError("CMake File API codemodel reply has no top-level paths object.")
        source_base = self._safe_file_api_path(paths.get("source"), base=".")
        build_base = self._safe_file_api_path(paths.get("build"), base=generated.relative_path)
        parsed: list[CMakeConfigurationTargets] = []
        for configuration in configurations:
            if not isinstance(configuration, Mapping):
                raise CMakeFileApiError("CMake File API configuration entries must be objects.")
            name = configuration.get("name")
            if not isinstance(name, str):
                raise CMakeFileApiError("CMake File API configuration names must be strings.")
            references = configuration.get("targets")
            if not isinstance(references, list):
                raise CMakeFileApiError("CMake File API configuration targets must be an array.")
            entries: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
            identifiers: dict[str, str] = {}
            for reference in references:
                if not isinstance(reference, Mapping):
                    raise CMakeFileApiError("CMake File API target references must be objects.")
                json_file = reference.get("jsonFile")
                if not isinstance(json_file, str) or not json_file:
                    raise CMakeFileApiError("A CMake File API target reference has no JSON object.")
                target = self._read_file_api_json(generated, f".cmake/api/v1/reply/{json_file}")
                target_id = target.get("id")
                target_name = target.get("name")
                if not isinstance(target_id, str) or not target_id or not isinstance(target_name, str) or not target_name:
                    raise CMakeFileApiError("A CMake File API target has no stable ID or name.")
                identifiers[target_id] = target_name
                entries.append((reference, target))
            targets = tuple(
                self._parse_target(target, identifiers, source_base=source_base, build_base=build_base)
                for _, target in entries
            )
            parsed.append(CMakeConfigurationTargets(name=name, targets=targets))
        return tuple(parsed)

    def _parse_target(
        self,
        target: Mapping[str, object],
        identifiers: Mapping[str, str],
        *,
        source_base: str,
        build_base: str,
    ) -> CMakeTargetMetadata:
        name = target.get("name")
        target_id = target.get("id")
        target_type = target.get("type")
        paths = target.get("paths")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(target_id, str)
            or not target_id
            or not isinstance(target_type, str)
            or not target_type
            or not isinstance(paths, Mapping)
        ):
            raise CMakeFileApiError("A CMake File API target is malformed.")
        target_source = self._safe_file_api_path(paths.get("source"), base=source_base)
        target_build = self._safe_file_api_path(paths.get("build"), base=build_base)
        artifacts = self._file_api_paths(target.get("artifacts"), base=target_build, key="path")
        sources = self._file_api_paths(target.get("sources"), base=target_source, key="path")
        raw_dependencies = target.get("dependencies", [])
        if not isinstance(raw_dependencies, list):
            raise CMakeFileApiError("CMake File API target dependencies must be an array.")
        dependencies: list[str] = []
        for dependency in raw_dependencies:
            if not isinstance(dependency, Mapping) or not isinstance(dependency.get("id"), str):
                raise CMakeFileApiError("CMake File API target dependency entries must identify a target ID.")
            dependency_id = dependency["id"]
            dependencies.append(identifiers.get(dependency_id, dependency_id))
        return CMakeTargetMetadata(
            name=name,
            target_id=target_id,
            type=target_type,
            build_directory=target_build,
            artifacts=artifacts,
            sources=sources,
            dependencies=tuple(dependencies),
        )

    def _file_api_paths(self, value: object, *, base: str, key: str) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise CMakeFileApiError("A CMake File API target path collection must be an array.")
        paths: list[str] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise CMakeFileApiError("A CMake File API target path entry must be an object.")
            paths.append(self._safe_file_api_path(item.get(key), base=base))
        return tuple(paths)

    def _safe_file_api_path(self, value: object, *, base: str) -> str:
        if not isinstance(value, str) or not value:
            raise CMakeFileApiError("CMake File API reported an invalid path.")
        try:
            return self._workspace.validate_reported_path(value, relative_to=base)
        except WorkspaceError as error:
            raise CMakeFileApiError("CMake File API reported a path outside the workspace or through a symlink.") from error

    @staticmethod
    def _parse_ctest_json(text: str) -> tuple[CTestTest, ...]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise CTestJsonError("CTest did not return valid json-v1 test metadata.") from error
        if not isinstance(payload, dict) or payload.get("kind") != "ctestInfo":
            raise CTestJsonError("CTest JSON does not identify ctestInfo metadata.")
        version = payload.get("version")
        tests = payload.get("tests")
        if not isinstance(version, dict) or version.get("major") != 1 or not isinstance(tests, list):
            raise CTestJsonError("CTest JSON is not supported json-v1 test metadata.")
        parsed: list[CTestTest] = []
        for item in tests:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
                raise CTestJsonError("CTest JSON test entries must have non-empty names.")
            parsed.append(CTestTest(name=item["name"]))
        return tuple(parsed)

    @staticmethod
    def _failed_tests(result: ProcessResult) -> tuple[str, ...]:
        """Extract only CTest's final failed-name section; non-zero remains a result."""
        names: list[str] = []
        in_summary = False
        for line in (result.stdout.text + "\n" + result.stderr.text).splitlines():
            if "The following tests FAILED:" in line:
                in_summary = True
                continue
            if not in_summary:
                continue
            match = _FAILED_TEST_LINE.match(line)
            if match is not None:
                name = match.group("name")
                if name not in names:
                    names.append(name)
            elif line.strip() and not line.startswith(" "):
                in_summary = False
        return tuple(names)
