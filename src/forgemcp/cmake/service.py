"""Transport-neutral CMake and CTest orchestration through ForgeMCP services."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import secrets
import hashlib
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Protocol, TypeAlias

from forgemcp.cmake.errors import (
    CMakeBuildTreeIncompatibleError,
    CMakeFileApiError,
    CMakeKitError,
    CMakeKitSelectionConflictError,
    CMakePresetError,
    CMakePresetKitConflictError,
    CMakeRequestError,
    CMakeToolUnavailableError,
    CompilationDatabaseRequirementError,
    CTestJsonError,
)
from forgemcp.cmake.models import (
    CMakeBuildPreset,
    CMakeBuildResult,
    CMakeBuildTree,
    CMakeConfigurationTargets,
    CMakeConfigurePreset,
    CMakeConfigureResult,
    CMakeDiagnostic,
    CMakePresetList,
    CMakeResolvedProfile,
    CMakeStatus,
    CMakeTargetList,
    CMakeTargetMetadata,
    CMakeTestPreset,
    CMakeToolStatus,
    CMakeVersion,
    CompilationDatabaseStatus,
    CTestRunResult,
    CTestTest,
    CTestTestList,
)
from forgemcp.core.config import ConfigurationSource, ForgeConfig
from forgemcp.models import ProcessOutput, ProcessResult
from forgemcp.processes import ProcessError
from forgemcp.processes import ProcessOutputObserver
from forgemcp.plugins import NoOpProgressReporter, ProgressUpdate, ToolExecutionContext
from forgemcp.cmake.progress import CMakeOutputProgressObserver, run_heartbeat, safe_progress_label
from forgemcp.cmake.events import CompilationDatabaseRegistry
from forgemcp.workspace import (
    GeneratedWorkspaceDirectory,
    WorkspaceError,
    WorkspaceFileNotFoundError,
    WorkspaceMutationBus,
    WorkspaceService,
)
from forgemcp.toolchain import (
    CMakeKit,
    CMakeKitList,
    CMakeKitSelection,
    ToolchainDiscoveryService,
    ToolchainProfile,
)


MINIMUM_CMAKE_VERSION = CMakeVersion(major=3, minor=23, patch=0, full="3.23.0")
"""First CMake release supported by the preset, File API, and CTest slice."""

MAX_PARALLEL_JOBS = 256
"""Largest explicit parallel build value accepted by the public API."""

_VERSION_BANNER = re.compile(r"\b(?:cmake|ctest)\s+version\s+(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)
_CACHE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FAILED_TEST_LINE = re.compile(r"^\s*\d+\s*-\s*(?P<name>.+?)\s+\([^)]*\)\s*$")
_CMAKE_GENERATOR_CACHE = re.compile(
    r"^CMAKE_GENERATOR(?::[^=]+)?=(?P<value>[^\r\n]{1,256})\r*$", re.MULTILINE
)
_CMAKE_HOME_DIRECTORY_CACHE = re.compile(
    r"^CMAKE_HOME_DIRECTORY(?::[^=]+)?=(?P<value>[^\r\n]{1,4096})\r*$", re.MULTILINE
)
_CMAKE_COMPILER_CACHE = re.compile(
    r"^CMAKE_(?:C|CXX)_COMPILER(?::[^=]+)?=(?P<value>[^\r\n]{1,4096})\r*$", re.MULTILINE
)
_COMPILER_DIAGNOSTIC = re.compile(
    r"^(?P<path>.+?):(?P<line>[0-9]+):(?P<column>[0-9]+):\s*"
    r"(?P<severity>fatal error|error|warning|note|remark):\s*(?P<message>.*)$",
    re.IGNORECASE,
)
_MSVC_DIAGNOSTIC = re.compile(
    r"^(?P<path>.+?)\((?P<line>[0-9]+)(?:,(?P<column>[0-9]+))?\):\s*"
    r"(?P<severity>fatal error|error|warning|note)\s*(?P<code>[A-Za-z]+[0-9]+)?\s*:?\s*(?P<message>.*)$",
    re.IGNORECASE,
)
_LINKER_DIAGNOSTIC = re.compile(
    r"^(?P<tool>LINK|(?:[^:]+/)?ld(?:\.exe)?)\s*:?\s*(?P<severity>fatal error|error|warning)\s*"
    r"(?P<code>(?:LNK)?[A-Za-z]*[0-9]+)?\s*:?\s*(?P<message>.*)$",
    re.IGNORECASE,
)
_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/(?!\s|/))[^\s'\"<>]*")
_SAFE_DIAGNOSTIC_CODE = re.compile(r"\[([A-Za-z][A-Za-z0-9_.-]{0,127})\]")

MAX_COMPILATION_DATABASE_BYTES = 4 * 1024 * 1024
MAX_COMPILATION_DATABASE_ENTRIES = 100_000
MAX_COMPILATION_DATABASE_DEPTH = 16
MAX_CACHED_DISCOVERY_PROFILES = 16
CACHED_DISCOVERY_PROFILE_TTL_SECONDS = 600.0

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
    configuration_stale: bool
    compilation_database: CompilationDatabaseStatus | None
    mutation_delivery_degraded: bool


@dataclass(frozen=True, slots=True)
class CachedCMakeTargetProfile:
    """One opaque application-local handle to already validated File API state."""

    profile_id: str
    targets: CMakeTargetList
    observed_at: datetime
    expires_at: float
    kit_id: str | None = None


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
        observer: ProcessOutputObserver | None = None,
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
        compilation_database: CompilationDatabaseRegistry | None = None,
        mutations: WorkspaceMutationBus | None = None,
    ) -> None:
        self._workspace = workspace
        self._process_runtime = process_runtime
        self._config = config
        self._toolchain = toolchain
        self._compilation_database_registry = compilation_database
        self._mutations = mutations
        self._cached_tool_status: CMakeStatus | None = None
        self._configured_binary_dir: str | None = None
        self._active_operations = 0
        self._last_configure: CMakeOperationStatusCache | None = None
        self._last_build: CMakeOperationStatusCache | None = None
        self._last_test: CMakeOperationStatusCache | None = None
        self._resolved_profile: CMakeResolvedProfile | None = None
        self._configuration_stale = False
        self._compilation_database: CompilationDatabaseStatus | None = None
        self._configured_toolchain_file: str | None = None
        self._configured_source_dir: str | None = None
        self._configured_compiler_family: str | None = None
        self._configured_kit_profile: ToolchainProfile | None = None
        self._selected_kit_incompatible = False
        self._last_relevant_mutation_generation = 0
        self._cached_presets: CMakePresetList | None = None
        self._cached_tests: dict[str, tuple[str, ...]] = {}
        self._target_profiles: OrderedDict[str, CachedCMakeTargetProfile] = OrderedDict()
        self._profile_by_binary_dir: dict[str, str] = {}
        self._selected_kit_id: str | None = None
        self._selection_generation = 0
        self._selection_lock = asyncio.Lock()

    def cached_project_status(self) -> CMakeProjectStatusCache:
        """Return content-free metadata without filesystem access or tool probes."""

        return CMakeProjectStatusCache(
            tool_status=self._cached_tool_status,
            configured_binary_dir=self._configured_binary_dir,
            active_operations=self._active_operations,
            last_configure=self._last_configure,
            last_build=self._last_build,
            last_test=self._last_test,
            configuration_stale=self._configuration_stale or self._selected_kit_incompatible or bool(self._mutations and self._mutations.degraded),
            compilation_database=self._compilation_database,
            mutation_delivery_degraded=bool(self._mutations and self._mutations.degraded),
        )

    def cached_target_profiles(self) -> tuple[CachedCMakeTargetProfile, ...]:
        """Return live opaque profiles without filesystem access or process work."""
        self._expire_target_profiles()
        return tuple(self._target_profiles.values())

    def cached_profile_ids(self, *, compatible_kit: str | None = None) -> tuple[str, ...]:
        """Return cached profile IDs, optionally restricted to their owning kit."""
        profiles = self.cached_target_profiles()
        return tuple(
            profile.profile_id for profile in profiles
            if compatible_kit is None or profile.kit_id == compatible_kit
        )

    def compatible_generators(self, kit: str | None = None) -> tuple[str, ...]:
        """Return cached qualified generators for a supplied/effective kit."""
        candidate, _, _ = self._effective_kit(kit)
        return () if candidate is None else candidate.compatible_generators

    def cached_target_profile(
        self, profile_id: str | None = None
    ) -> CachedCMakeTargetProfile | None:
        """Resolve one application-local profile, defaulting to the latest cache."""
        self._expire_target_profiles()
        if profile_id is None:
            latest = next(reversed(self._target_profiles), None)
            return None if latest is None else self._target_profiles[latest]
        return self._target_profiles.get(profile_id)

    def cached_configurations(self, profile_id: str | None = None) -> tuple[str, ...]:
        profile = self.cached_target_profile(profile_id)
        if profile is None:
            return ()
        return tuple(
            sorted(
                {configuration.name for configuration in profile.targets.configurations},
                key=lambda value: (value.casefold(), value),
            )
        )

    def cached_target_names(
        self, profile_id: str | None = None, configuration: str | None = None
    ) -> tuple[str, ...]:
        profile = self.cached_target_profile(profile_id)
        if profile is None:
            return ()
        names = {
            target.name
            for item in profile.targets.configurations
            if configuration is None or item.name == configuration
            for target in item.targets
        }
        return tuple(sorted(names, key=lambda value: (value.casefold(), value)))

    def cached_test_names(self, profile_id: str | None = None) -> tuple[str, ...]:
        profile = self.cached_target_profile(profile_id)
        if profile is None:
            return ()
        return tuple(
            sorted(
                set(self._cached_tests.get(profile.targets.binary_dir, ())),
                key=lambda value: (value.casefold(), value),
            )
        )

    def cached_preset_names(self) -> tuple[str, ...]:
        if self._cached_presets is None:
            return ()
        names = {preset.name for preset in self._cached_presets.configure_presets}
        return tuple(sorted(names, key=lambda value: (value.casefold(), value)))

    def list_kits(self) -> CMakeKitList:
        """Return cached path-free kit metadata without discovery refresh or probes."""
        if self._toolchain is None:
            return CMakeKitList(kits=(), discovery_state="unavailable", complete=False)
        return self._toolchain.kits()

    async def select_kit(
        self, kit: str, *, expected_selection_generation: int | None = None
    ) -> CMakeKitSelection:
        """Select one qualified kit without configure, cache deletion, or env mutation."""
        if not isinstance(kit, str) or not kit or len(kit) > 96:
            raise CMakeKitError("The requested kit must be a bounded opaque kit identifier.")
        async with self._selection_lock:
            if expected_selection_generation is not None:
                if (
                    isinstance(expected_selection_generation, bool)
                    or not isinstance(expected_selection_generation, int)
                    or expected_selection_generation < 0
                ):
                    raise CMakeKitSelectionConflictError(
                        "expected_selection_generation must be a non-negative integer."
                    )
                if expected_selection_generation != self._selection_generation:
                    raise CMakeKitSelectionConflictError(
                        "The CMake kit selection changed concurrently; refresh selection and retry."
                    )
            candidate = self._kit_by_id(kit)
            if candidate is None or candidate.readiness == "rejected":
                raise CMakeKitError(
                    "The requested CMake kit is unavailable or not qualified; selection was left unchanged."
                )
            self._selected_kit_id = candidate.id
            self._selection_generation += 1
            self._selected_kit_incompatible = (
                self._configured_compiler_family is not None
                and self._configured_compiler_family != candidate.compiler_family
            )
            return self._kit_selection()

    def clear_selection(self) -> None:
        """Release application-session selection during plugin/application shutdown."""
        self._selected_kit_id = None
        self._selected_kit_incompatible = False

    def _kit_selection(self, *, operation_kit: str | None = None) -> CMakeKitSelection:
        kit, source, _ = self._effective_kit(operation_kit)
        return CMakeKitSelection(
            selected_kit=self._selected_kit_id,
            effective_kit=kit,
            selection_generation=self._selection_generation,
            source=source,
        )

    def _kit_by_id(self, kit_id: str | None) -> CMakeKit | None:
        return None if kit_id is None or self._toolchain is None else self._toolchain.kit(kit_id)

    def _effective_kit(
        self, operation_kit: str | None = None
    ) -> tuple[CMakeKit | None, str, ToolchainProfile | None]:
        """Apply operation → runtime → CLI/env initial config → automatic precedence."""
        initial = None if self._config is None else self._config.cmake_kit
        for candidate_id, source in (
            (operation_kit, "operation"),
            (self._selected_kit_id, "runtime"),
            (initial, self._initial_kit_source()),
        ):
            if candidate_id is None:
                continue
            kit = self._kit_by_id(candidate_id)
            if kit is None or kit.readiness == "rejected":
                if source == "operation":
                    raise CMakeKitError("The requested CMake kit is unavailable or not qualified.")
                continue
            return kit, source, self._toolchain.kit_profile(kit.id) if self._toolchain else None
        if self._toolchain is not None:
            kit = next((item for item in self._toolchain.kits().kits if item.readiness == "ready"), None)
            if kit is not None:
                return kit, "automatic", self._toolchain.kit_profile(kit.id)
        return None, "none", None

    def _initial_kit_source(self) -> str:
        if self._config is None or self._config.cmake_kit is None:
            return "none"
        source = self._config.source_of("cmake_kit").value
        return source if source in {"cli", "environment"} else "configuration"

    def _kit_is_explicit(self, operation_kit: str | None = None) -> bool:
        return operation_kit is not None or self._selected_kit_id is not None or bool(
            self._config is not None and self._config.cmake_kit is not None
        )

    def list_build_trees(self, *, source_dir: str | None = None) -> tuple[CMakeBuildTree, ...]:
        """Boundedly inspect conventional workspace build directories read-only.

        The scan deliberately enumerates only named CMake build patterns.  It
        never recursively walks the workspace, follows a link/reparse point,
        returns a cache variable, or invokes CMake.
        """
        source = self._workspace.require_directory(
            source_dir if source_dir is not None else self._config.cmake_source_dir if self._config else "."
        )
        candidates: set[str] = {"build"}
        if self._config is not None and self._config.build_dir is not None:
            candidates.add(self._config.build_dir)
        if self._configured_binary_dir is not None:
            candidates.add(self._configured_binary_dir)
        candidates.update(self._profile_by_binary_dir)
        root = self._workspace.workspace_root
        try:
            direct = sorted(os.scandir(root), key=lambda entry: entry.name.casefold())
        except OSError:
            direct = []
        for entry in direct[:128]:
            if not entry.is_dir(follow_symlinks=False):
                continue
            name = entry.name
            if name.startswith("build-") or name.startswith("cmake-build-"):
                candidates.add(name)
            if name == "out":
                try:
                    out_build = sorted(os.scandir(Path(entry.path) / "build"), key=lambda item: item.name.casefold())
                except OSError:
                    out_build = []
                for child in out_build[:64]:
                    if child.is_dir(follow_symlinks=False):
                        candidates.add(f"out/build/{child.name}")
        trees: list[CMakeBuildTree] = []
        for candidate in sorted(candidates, key=lambda value: (value.casefold(), value))[:64]:
            try:
                generated = self._workspace.open_generated_directory(candidate)
                cache = generated.read_text("CMakeCache.txt")
            except WorkspaceError:
                continue
            tree = self._build_tree_summary(generated, cache, source)
            if tree is not None:
                trees.append(tree)
        return tuple(trees)

    def _build_tree_summary(
        self, generated: GeneratedWorkspaceDirectory, cache: str, source: str
    ) -> CMakeBuildTree | None:
        home = _CMAKE_HOME_DIRECTORY_CACHE.search(cache)
        source_matches = False
        if home is not None:
            try:
                source_matches = self._workspace.validate_reported_path(home.group("value")) == source
            except WorkspaceError:
                source_matches = False
        generator = self._cached_generator_from_text(cache)
        family = self._cached_compiler_family_from_text(cache)
        kit, selection_source, _ = self._effective_kit()
        compatibility = "unknown"
        if kit is not None and selection_source != "automatic":
            compatibility = (
                "compatible"
                if family in {None, kit.compiler_family}
                and (generator is None or generator in kit.compatible_generators)
                else "incompatible"
            )
        stale = self._configuration_stale and generated.relative_path == self._configured_binary_dir
        database = self._validate_compilation_database(generated)
        file_api_valid = source_matches and self._file_api_is_valid(generated)
        category = (
            "incompatible" if not source_matches or compatibility == "incompatible"
            else "adoptable" if file_api_valid
            else "buildable"
        )
        profile_id = "tree-" + hashlib.sha256(generated.relative_path.encode("utf-8")).hexdigest()[:20]
        return CMakeBuildTree(
            profile_id=profile_id,
            binary_dir=generated.relative_path,
            source_matches_workspace=source_matches,
            generator=generator,
            compiler_family=family,
            configuration_type=(
                "multi_config" if generator is not None and not self._generator_is_single_config(generator)
                else "single_config" if generator is not None else "unknown"
            ),
            compilation_database=database,
            stale=stale,
            selected_kit_compatibility="stale" if stale and compatibility == "compatible" else compatibility,
            category=category,
        )

    @staticmethod
    def _cached_generator_from_text(cache: str) -> str | None:
        match = _CMAKE_GENERATOR_CACHE.search(cache)
        return None if match is None else match.group("value")

    @staticmethod
    def _cached_compiler_family_from_text(cache: str) -> str | None:
        match = _CMAKE_COMPILER_CACHE.search(cache)
        if match is None:
            return None
        value = match.group("value").replace("\\", "/").casefold()
        name = value.rsplit("/", 1)[-1]
        if name in {"cl.exe", "cl"}:
            return "msvc"
        if "clang-cl" in name:
            return "clang-cl"
        if "clang" in name:
            return "clang"
        if name.startswith(("g++", "gcc")):
            return "gcc"
        return None

    def _safe_diagnostics(
        self, result: ProcessResult, *, category: str
    ) -> tuple[tuple[CMakeDiagnostic, ...], int, int, bool]:
        """Parse compiler-style output once into bounded path-safe diagnostics.

        Raw process output remains an execution implementation detail for older
        result compatibility; this parser never forwards source/caret lines,
        command text, external locations, or absolute paths embedded in a
        message.
        """
        diagnostics: list[CMakeDiagnostic] = []
        omitted = 0
        invalid = 0
        for stream in (result.stdout.text, result.stderr.text):
            for raw in stream.splitlines():
                match = _COMPILER_DIAGNOSTIC.match(raw) or _MSVC_DIAGNOSTIC.match(raw)
                if match is None:
                    linker = _LINKER_DIAGNOSTIC.match(raw)
                    if linker is not None and len(diagnostics) < 64:
                        message = _ABSOLUTE_PATH.sub("<external-path>", linker.group("message").strip())[:1024] or "linker diagnostic"
                        diagnostics.append(CMakeDiagnostic(
                            category="linker", severity="error" if "error" in linker.group("severity").casefold() else "warning",
                            message=message, code=linker.group("code"),
                        ))
                    continue
                try:
                    relative = self._workspace.validate_reported_path(match.group("path"))
                except WorkspaceError:
                    omitted += 1
                    continue
                if len(diagnostics) >= 64:
                    invalid += 1
                    continue
                try:
                    line = int(match.group("line")) - 1
                    column = int(match.group("column") or "1") - 1
                except ValueError:
                    invalid += 1
                    continue
                if line < 0 or column < 0:
                    invalid += 1
                    continue
                severity_text = match.group("severity").casefold()
                severity = "error" if "error" in severity_text else "warning" if severity_text == "warning" else "information"
                message = _ABSOLUTE_PATH.sub("<external-path>", match.group("message").strip())
                message = message[:1024] or "compiler diagnostic"
                code_match = _SAFE_DIAGNOSTIC_CODE.search(message)
                raw_code = match.groupdict().get("code")
                diagnostic_category = (
                    "linker" if any(token in message.casefold() for token in ("linker", "undefined reference", "lnk"))
                    else "compiler" if category == "build" else "configure"
                )
                diagnostics.append(CMakeDiagnostic(
                    category=diagnostic_category, severity=severity, message=message,
                    code=(raw_code[:128] if raw_code else None) if code_match is None else code_match.group(1),
                    file=relative, line=line, column=column,
                ))
        complete = not result.stdout.truncated and not result.stderr.truncated and invalid == 0
        return tuple(diagnostics), omitted, invalid, complete

    @staticmethod
    def _safe_process_result(result: ProcessResult) -> ProcessResult:
        """Retain execution metadata but never send raw configure/build streams."""
        return result.model_copy(update={
            "stdout": ProcessOutput(text="", truncated=result.stdout.truncated),
            "stderr": ProcessOutput(text="", truncated=result.stderr.truncated),
        })

    @staticmethod
    def _build_outcome(result: ProcessResult, diagnostics: Sequence[CMakeDiagnostic]) -> str:
        if result.timed_out:
            return "timeout"
        if result.exit_code == 0:
            return "success_with_warnings" if any(item.severity == "warning" for item in diagnostics) else "success"
        if any(item.category == "linker" for item in diagnostics):
            return "linker_failure"
        if any(item.category == "compiler" for item in diagnostics):
            return "compile_failure"
        return "build_system_failure"

    @staticmethod
    def _configure_category(result: ProcessResult) -> str | None:
        if result.timed_out:
            return "timeout"
        if result.exit_code == 0:
            return None
        text = (result.stdout.text + "\n" + result.stderr.text).casefold()
        categories = (
            ("compiler_not_found", ("could not find compiler", "compiler was not found")),
            ("compiler_test_failed", ("test program", "compiler is not able to compile")),
            ("compiler_abi_mismatch", ("abi", "machine type")),
            ("generator_mismatch", ("does not match the generator",)),
            ("build_tool_missing", ("build program", "ninja")),
            ("linker_not_found", ("linker", "link.exe", "ld.exe")),
            ("windows_sdk_missing", ("windows sdk", "windows kits")),
            ("environment_incomplete", ("vcvars", "visual studio environment")),
            ("invalid_existing_cache", ("cmakecache", "cache")),
        )
        for category, markers in categories:
            if any(marker in text for marker in markers):
                return category
        return "project_configure_error"

    @property
    def cached_targets_stale(self) -> bool:
        """Whether cached target metadata may predate relevant Workspace changes."""
        return self._configuration_stale or self._selected_kit_incompatible or bool(self._mutations and self._mutations.degraded)

    async def status(self) -> CMakeStatus:
        """Discover CMake and CTest and report their parseable, supported versions."""
        cmake = await self._tool_status("cmake", requires_minimum=True)
        ctest = await self._tool_status("ctest", requires_minimum=False)
        effective_kit, _, _ = self._effective_kit()
        profile = self._resolve_profile(
            binary_dir=None, source_dir=None, preset=None,
            explicit_kit=effective_kit if self._kit_is_explicit() else None,
        )
        self._resolved_profile = profile
        status = CMakeStatus(
            available=cmake.available and cmake.supported and ctest.available,
            minimum_cmake_version=MINIMUM_CMAKE_VERSION,
            cmake=cmake,
            ctest=ctest,
            profile=profile,
            compilation_database=self._compilation_database,
            kit_selection=self._kit_selection(),
            selected_kit_compatibility=self._selected_profile_kit_compatibility(),
            warnings=self._status_warnings(),
        )
        self._cached_tool_status = status
        return status

    def _selected_profile_kit_compatibility(self) -> str:
        """Compare cached profile facts only; status never reads a cache or probes."""
        if self._configured_binary_dir is None:
            return "unknown"
        kit, source, _ = self._effective_kit()
        if kit is None or source == "automatic":
            return "unknown"
        if self._configuration_stale or self._selected_kit_incompatible:
            return "stale"
        # Configure writes this private safe fact only after a successful
        # result; old/externally discovered trees are assessed by list_build_trees.
        return "compatible" if getattr(self, "_configured_compiler_family", None) in {None, kit.compiler_family} else "incompatible"

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
        result = CMakePresetList(
            source_dir=source,
            preset_files=tuple(preset_files),
            configure_presets=tuple(configure),
            build_presets=tuple(build),
            test_presets=tuple(test),
        )
        self._cached_presets = result
        return result

    async def configure(
        self,
        *,
        source_dir: str | None = None,
        binary_dir: str | None = None,
        preset: str | None = None,
        kit: str | None = None,
        generator: str | None = None,
        cache_variables: Mapping[str, CacheValue] | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> CMakeConfigureResult:
        """Configure a safe generated build directory using a preset or direct mode."""
        started = monotonic()
        context = self._execution_context(execution_context)
        self._active_operations += 1
        normalised_binary = "."
        configure_generation = self._mutations.generation if self._mutations is not None else 0
        progress_observer: CMakeOutputProgressObserver | None = None
        try:
            context.throw_if_cancelled()
            await context.report_progress(ProgressUpdate(0, None, "Preparing configure"))
            preset_name = self._selected_preset(preset)
            effective_kit, kit_source, kit_profile = self._effective_kit(kit)
            if preset_name is not None and self._kit_is_explicit(kit):
                raise CMakePresetKitConflictError(
                    "A configure preset and an explicit ForgeMCP kit are alternative toolchain workflows; choose preset-owned or kit-owned toolchain selection."
                )
            profile = self._resolve_profile(
                binary_dir=binary_dir, source_dir=source_dir, preset=preset,
                explicit_kit=effective_kit if self._kit_is_explicit(kit) else None,
            )
            await context.report_progress(ProgressUpdate(0, None, "Resolving toolchain and preset"))
            source = self._workspace.require_directory(profile.source_dir)
            generated = self._workspace.open_generated_directory(profile.binary_dir, create=True)
            normalised_binary = generated.relative_path
            if generated.relative_path == source:
                raise CMakeRequestError("CMake source_dir and binary_dir must be different directories.")
            existing_generator = self._cached_generator(generated)
            requested_generator = self._requested_generator(
                generator, preset_name, existing_generator, generated, effective_kit
            )
            known_generator = requested_generator or self._preset_generator(source, preset_name)
            self._preflight_generator(
                existing_generator, known_generator,
                kit_id=None if effective_kit is None else effective_kit.id,
            )
            cached_family = self._cached_compiler_family(generated)
            if (
                cached_family is not None
                and effective_kit is not None
                and self._kit_is_explicit(kit)
                and cached_family != effective_kit.compiler_family
            ):
                raise CMakeBuildTreeIncompatibleError(
                    self._build_tree_incompatibility_message(
                        existing_generator, known_generator, cached_family, effective_kit.compiler_family,
                        effective_kit.id,
                    )
                )
            generated.write_text(".cmake/api/v1/query/codemodel-v2", "")

            argv = [self._tool_executable("cmake"), "-S", source, "-B", generated.relative_path]
            if preset_name is not None:
                argv.extend(["--preset", preset_name])
            elif requested_generator is not None and existing_generator is None:
                argv.extend(["-G", requested_generator])
                if (
                    self._config is not None
                    and self._config.target_arch != "auto"
                    and requested_generator.casefold().startswith("visual studio")
                ):
                    argv.extend(["-A", self._config.target_arch])
            if (
                effective_kit is not None
                and kit_profile is not None
                and self._generator_is_command_line(known_generator or existing_generator)
            ):
                if kit_profile.c_compiler_path is None or kit_profile.cxx_compiler_path is None:
                    raise CMakeKitError("The selected kit has no qualified C/C++ compiler pair.")
                argv.extend((
                    f"-DCMAKE_C_COMPILER:FILEPATH={kit_profile.c_compiler_path}",
                    f"-DCMAKE_CXX_COMPILER:FILEPATH={kit_profile.cxx_compiler_path}",
                ))
            argv.extend(self._configure_cache_arguments(cache_variables, known_generator or existing_generator))
            await context.report_progress(ProgressUpdate(0, None, "Configure started"))
            result, progress_observer = await self._run_with_progress(
                "cmake", argv, timeout_seconds=self._default_timeout("configure"),
                context=context, operation="configure", kit_profile=kit_profile,
            )
            response = CMakeConfigureResult(
                source_dir=source,
                binary_dir=generated.relative_path,
                preset=preset_name,
                process=self._safe_process_result(result),
                effective_kit=effective_kit,
                generator=known_generator or existing_generator,
                compiler_family=None if effective_kit is None else effective_kit.compiler_family,
                diagnostic_category=self._configure_category(result),
                diagnostics=self._safe_diagnostics(result, category="configure")[0],
            )
        except asyncio.CancelledError:
            self._last_configure = self._operation_cache("configure", "cancelled", normalised_binary, started)
            await context.report_progress(ProgressUpdate(0, None, "Configure cancelled", terminal=True))
            raise
        except Exception:
            self._last_configure = self._operation_cache("configure", "failure", normalised_binary, started)
            await context.report_progress(ProgressUpdate(0, None, "Configure failed", terminal=True))
            raise
        else:
            outcome = "success" if result.exit_code == 0 and not result.timed_out else "failure"
            self._last_configure = self._operation_cache(
                "configure", outcome, generated.relative_path, started, exit_code=result.exit_code
            )
            if outcome == "success":
                # A process exit alone does not establish that ForgeMCP can
                # consume CMake's generated model.  Keep process success but
                # expose a fixed warning if its bounded File API validation
                # fails, just like an optional compilation database.
                file_api_valid = self._file_api_is_valid(generated)
                database = self._validate_compilation_database(generated)
                self._compilation_database = database
                self._configured_toolchain_file = self._toolchain_file_from_request(cache_variables, source)
                self._configured_source_dir = source
                self._configured_compiler_family = None if effective_kit is None else effective_kit.compiler_family
                # Build and CTest must use the same privately retained,
                # already-qualified Developer environment as configure.  The
                # profile is an application-local capability and never enters
                # an MCP model, log event, or subprocess argv.
                self._configured_kit_profile = kit_profile
                self._selected_kit_incompatible = False
                if self._compile_commands_mode == "required" and database.availability != "available":
                    self._last_configure = self._operation_cache(
                        "configure", "requirement_failed", generated.relative_path, started,
                        exit_code=result.exit_code,
                    )
                    raise CompilationDatabaseRequirementError(
                        "CMake configured the build tree, but the required compile_commands.json database is unavailable or invalid."
                    )
                self._configured_binary_dir = generated.relative_path
                self._resolved_profile = CMakeResolvedProfile(
                    source_dir=source,
                    binary_dir=generated.relative_path,
                    source_dir_source=profile.source_dir_source,
                    binary_dir_source=profile.binary_dir_source,
                    configure_preset_source=profile.configure_preset_source,
                )
                self._refresh_configuration_staleness_since(configure_generation)
                if self._compilation_database_registry is not None:
                    await self._compilation_database_registry.publish(database)
                # A mutation can commit while database consumers are running.
                # The final generation sample is the configure completion
                # boundary; a later batch therefore cannot be cleared by this
                # successful configure result.
                self._refresh_configuration_staleness_since(configure_generation)
                warnings = self._configure_warnings(database, file_api_valid=file_api_valid)
                response = CMakeConfigureResult(
                    source_dir=source,
                    binary_dir=generated.relative_path,
                    preset=preset_name,
                    process=self._safe_process_result(result),
                    compilation_database=database,
                    effective_kit=effective_kit,
                    generator=self._actual_generator(generated) or known_generator,
                    compiler_family=None if effective_kit is None else effective_kit.compiler_family,
                    diagnostic_category=self._configure_category(result),
                    diagnostics=self._safe_diagnostics(result, category="configure")[0],
                    warnings=warnings,
                )
                message = "Configure completed" if not warnings else "Configure completed with warnings"
                await context.report_progress(
                    (progress_observer or CMakeOutputProgressObserver(context, "configure")).terminal_success_update(message)
                )
            else:
                message = "Configure timed out" if result.timed_out else "Configure failed"
                await context.report_progress(ProgressUpdate(0, None, message, terminal=True))
            return response
        finally:
            self._active_operations -= 1

    def list_targets(self, *, binary_dir: str | None = None) -> CMakeTargetList:
        """Read target metadata solely from CMake File API codemodel v2."""
        kit, _, _ = self._effective_kit()
        generated = self._workspace.open_generated_directory(self._resolve_profile(
            binary_dir=binary_dir, source_dir=None, preset=None,
            explicit_kit=kit if self._kit_is_explicit() else None,
        ).binary_dir)
        codemodel = self._load_codemodel(generated)
        configurations = self._parse_target_configurations(codemodel, generated)
        result = CMakeTargetList(binary_dir=generated.relative_path, configurations=configurations)
        self._cache_target_profile(result)
        return result

    async def build(
        self,
        *,
        binary_dir: str | None = None,
        targets: Iterable[str] = (),
        configuration: str | None = None,
        parallel_jobs: int | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> CMakeBuildResult:
        """Build the default project or explicit target names without invoking a shell."""
        started = monotonic()
        context = self._execution_context(execution_context)
        self._active_operations += 1
        normalised_binary = "."
        target_names: tuple[str, ...] = ()
        progress_observer: CMakeOutputProgressObserver | None = None
        try:
            context.throw_if_cancelled()
            await context.report_progress(ProgressUpdate(0, None, "Preparing build"))
            kit, _, kit_profile = self._effective_kit()
            profile = self._resolve_profile(
                binary_dir=binary_dir, source_dir=None, preset=None,
                explicit_kit=kit if self._kit_is_explicit() else None,
            )
            generated = self._workspace.open_generated_directory(profile.binary_dir)
            normalised_binary = generated.relative_path
            target_names = self._validate_names(targets, label="target")
            selected_configuration = self._selected_configuration(configuration)
            jobs = self._validate_parallel_jobs(parallel_jobs)
            if target_names:
                display = self._safe_progress_target(target_names[0])
                message = f"Selected {len(target_names)} target" if len(target_names) == 1 else f"Selected {len(target_names)} targets"
                if display is not None and len(target_names) == 1:
                    message = f"Selected target: {display}"
                await context.report_progress(ProgressUpdate(0, None, message))
            else:
                await context.report_progress(ProgressUpdate(0, None, "Selected default build targets"))
            argv = [self._tool_executable("cmake"), "--build", generated.relative_path]
            if target_names:
                argv.extend(["--target", *target_names])
            if selected_configuration is not None:
                argv.extend(["--config", selected_configuration])
            if jobs is not None:
                argv.extend(["--parallel", str(jobs)])
            await context.report_progress(ProgressUpdate(0, None, "Build started"))
            result, progress_observer = await self._run_with_progress(
                "cmake", argv, timeout_seconds=self._default_timeout("build"),
                context=context, operation="build",
                kit_profile=kit_profile or self._configured_kit_profile,
            )
            diagnostics, omitted, invalid, complete = self._safe_diagnostics(result, category="build")
            response = CMakeBuildResult(
                binary_dir=generated.relative_path,
                targets=target_names,
                configuration=selected_configuration,
                process=self._safe_process_result(result),
                outcome=self._build_outcome(result, diagnostics),
                diagnostics=diagnostics,
                omitted_external_diagnostics=omitted,
                invalid_diagnostics=invalid,
                complete=complete,
            )
        except asyncio.CancelledError:
            self._last_build = self._operation_cache("build", "cancelled", normalised_binary, started, item_count=len(target_names))
            await context.report_progress(ProgressUpdate(0, None, "Build cancelled", terminal=True))
            raise
        except Exception:
            self._last_build = self._operation_cache("build", "failure", normalised_binary, started, item_count=len(target_names))
            await context.report_progress(ProgressUpdate(0, None, "Build failed", terminal=True))
            raise
        else:
            outcome = "success" if result.exit_code == 0 and not result.timed_out else "failure"
            self._last_build = self._operation_cache("build", outcome, generated.relative_path, started, exit_code=result.exit_code, item_count=len(target_names))
            if outcome == "success":
                await context.report_progress(
                    (progress_observer or CMakeOutputProgressObserver(context, "build")).terminal_success_update("Build completed")
                )
            else:
                message = "Build timed out" if result.timed_out else "Build failed"
                await context.report_progress(ProgressUpdate(0, None, message, terminal=True))
            return response
        finally:
            self._active_operations -= 1

    async def list_tests(self, *, binary_dir: str | None = None) -> CTestTestList:
        """List tests through CTest's documented ``json-v1`` output format."""
        kit, _, kit_profile = self._effective_kit()
        generated = self._workspace.open_generated_directory(self._resolve_profile(
            binary_dir=binary_dir, source_dir=None, preset=None,
            explicit_kit=kit if self._kit_is_explicit() else None,
        ).binary_dir)
        result = await self._run_required(
            "ctest", [self._tool_executable("ctest"), "--test-dir", generated.relative_path, "--show-only=json-v1"],
            timeout_seconds=self._default_timeout("test"),
            kit_profile=kit_profile or self._configured_kit_profile,
        )
        if result.exit_code != 0 or result.timed_out:
            raise CTestJsonError("CTest could not produce a JSON test listing for this build directory.")
        tests = self._parse_ctest_json(result.stdout.text)
        response = CTestTestList(binary_dir=generated.relative_path, tests=tests, process=result)
        self._cached_tests[generated.relative_path] = tuple(test.name for test in tests)
        return response

    async def run_tests(
        self,
        *,
        binary_dir: str | None = None,
        test_names: Iterable[str] = (),
        configuration: str | None = None,
        timeout_seconds: float | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> CTestRunResult:
        """Run all tests or an exact-name subset, with ProcessRuntime limits in force."""
        started = monotonic()
        context = self._execution_context(execution_context)
        self._active_operations += 1
        normalised_binary = "."
        names: tuple[str, ...] = ()
        progress_observer: CMakeOutputProgressObserver | None = None
        try:
            context.throw_if_cancelled()
            await context.report_progress(ProgressUpdate(0, None, "Preparing test run"))
            kit, _, kit_profile = self._effective_kit()
            profile = self._resolve_profile(
                binary_dir=binary_dir, source_dir=None, preset=None,
                explicit_kit=kit if self._kit_is_explicit() else None,
            )
            generated = self._workspace.open_generated_directory(profile.binary_dir)
            normalised_binary = generated.relative_path
            names = self._validate_names(test_names, label="test name")
            selected_configuration = self._selected_configuration(configuration)
            await context.report_progress(
                ProgressUpdate(0, None, "Preparing selected tests" if names else "Preparing discovered tests")
            )
            argv = [self._tool_executable("ctest"), "--test-dir", generated.relative_path, "--output-on-failure"]
            if selected_configuration is not None:
                argv.extend(["--build-config", selected_configuration])
            if names:
                argv.extend(["-R", "^(?:" + "|".join(re.escape(name) for name in names) + ")$"])
            await context.report_progress(ProgressUpdate(0, None, "Test run started"))
            result, progress_observer = await self._run_with_progress(
                "ctest",
                argv,
                timeout_seconds=(
                    self._default_timeout("test")
                    if timeout_seconds is None
                    else timeout_seconds
                ),
                context=context,
                operation="test",
                kit_profile=kit_profile or self._configured_kit_profile,
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
            await context.report_progress(ProgressUpdate(0, None, "Test run cancelled", terminal=True))
            raise
        except Exception:
            self._last_test = self._operation_cache("test", "failure", normalised_binary, started, item_count=len(names))
            await context.report_progress(ProgressUpdate(0, None, "Test run failed", terminal=True))
            raise
        else:
            outcome = "success" if result.exit_code == 0 and not result.timed_out else "failure"
            self._last_test = self._operation_cache("test", outcome, generated.relative_path, started, exit_code=result.exit_code, item_count=len(names) if names else len(failed_tests))
            await context.report_progress(ProgressUpdate(0, None, "Finishing test run"))
            if outcome == "success":
                await context.report_progress(
                    (progress_observer or CMakeOutputProgressObserver(context, "test")).terminal_success_update("Test run completed")
                )
            else:
                message = "Test run timed out" if result.timed_out else "Test run failed"
                await context.report_progress(ProgressUpdate(0, None, message, terminal=True))
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
        self,
        executable: str,
        argv: Sequence[str],
        *,
        timeout_seconds: float | None = None,
        observer: ProcessOutputObserver | None = None,
        kit_profile: ToolchainProfile | None = None,
    ) -> ProcessResult:
        """Run a fixed executable and make runtime absence a safe CMake domain error."""
        try:
            runner = self._process_runtime.run
            use_profile_runner = (
                kit_profile is not None
                and hasattr(self._process_runtime, "run_cmake_toolchain")
            )
            if use_profile_runner:
                runner = getattr(self._process_runtime, "run_cmake_toolchain")
            elif self._toolchain is not None and hasattr(self._process_runtime, "run_toolchain"):
                runner = getattr(self._process_runtime, "run_toolchain")
            arguments: dict[str, object] = {"cwd": ".", "timeout_seconds": timeout_seconds}
            if use_profile_runner:
                arguments["environment"] = kit_profile.environment
            if observer is not None and self._runner_accepts_observer(runner):
                arguments["observer"] = observer
            return await runner(argv, **arguments)
        except ProcessError as error:
            raise CMakeToolUnavailableError(
                f"{executable} is not available through the configured Process Runtime."
            ) from error

    async def _run_with_progress(
        self,
        executable: str,
        argv: Sequence[str],
        *,
        timeout_seconds: float | None,
        context: ToolExecutionContext,
        operation: str,
        kit_profile: ToolchainProfile | None = None,
    ) -> tuple[ProcessResult, CMakeOutputProgressObserver]:
        """Run a command with one bounded observer worker and one heartbeat task."""
        observer = CMakeOutputProgressObserver(context, operation)
        heartbeat: asyncio.Task[None] | None = None
        if context.supports_progress:
            heartbeat = asyncio.create_task(run_heartbeat(context, operation=operation))
        try:
            result = await self._run_required(
                executable, argv, timeout_seconds=timeout_seconds,
                observer=observer if context.supports_progress else None, kit_profile=kit_profile,
            )
            return result, observer
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    @staticmethod
    def _runner_accepts_observer(runner: object) -> bool:
        """Keep Phase-A fake/external runners source-compatible with the new option."""
        try:
            signature = inspect.signature(runner)
        except (TypeError, ValueError):
            return False
        return "observer" in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )

    @staticmethod
    def _execution_context(value: ToolExecutionContext | None) -> ToolExecutionContext:
        return value if value is not None else ToolExecutionContext(NoOpProgressReporter())

    @staticmethod
    def _safe_progress_target(value: str) -> str | None:
        return safe_progress_label(value)

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
        self, *, binary_dir: str | None, source_dir: str | None, preset: str | None,
        explicit_kit: CMakeKit | None = None,
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
            elif explicit_kit is not None:
                # A selected kit must not silently reuse the legacy tree of a
                # different generator/compiler family.  The opaque ID is safe
                # to put in this workspace-relative deterministic suggestion.
                binary, binary_origin = f"build/forgemcp/{explicit_kit.id}", "kit"
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

    def _preset_generator(self, source_dir: str, preset: str | None) -> str | None:
        """Read only a directly declared preset generator for safe preflight."""
        if preset is None:
            return None
        source = self._workspace.require_directory(source_dir)
        for filename in ("CMakePresets.json", "CMakeUserPresets.json"):
            path = filename if source == "." else f"{source}/{filename}"
            try:
                document = self._parse_preset_document(self._workspace.read_text(path)[0], path)
            except WorkspaceFileNotFoundError:
                continue
            for item in self._preset_entries(document, "configurePresets"):
                if item.get("name") != preset:
                    continue
                value = item.get("generator")
                if value is None:
                    return None
                if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
                    raise CMakePresetError("The selected configure preset has an unsafe generator.")
                return value
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

    @property
    def _compile_commands_mode(self) -> str:
        """Return the composed policy; config-less unit composition keeps legacy neutrality."""
        return "off" if self._config is None else self._config.compile_commands

    def _configure_cache_arguments(
        self, cache_variables: Mapping[str, CacheValue] | None, requested_generator: str | None
    ) -> tuple[str, ...]:
        """Apply operation cache precedence and the database export policy."""
        values: dict[str, CacheValue] = {} if cache_variables is None else dict(cache_variables)
        export = values.get("CMAKE_EXPORT_COMPILE_COMMANDS")
        if self._compile_commands_mode == "required" and self._cache_value_is_off(export):
            raise CMakeRequestError(
                "compile_commands=required conflicts with explicit CMAKE_EXPORT_COMPILE_COMMANDS=OFF."
            )
        if self._compile_commands_mode != "off" and "CMAKE_EXPORT_COMPILE_COMMANDS" not in values:
            values["CMAKE_EXPORT_COMPILE_COMMANDS"] = True
        if (
            requested_generator is not None
            and self._generator_is_single_config(requested_generator)
            and self._config is not None
            and self._config.default_configuration is not None
            and "CMAKE_BUILD_TYPE" not in values
        ):
            values["CMAKE_BUILD_TYPE"] = self._config.default_configuration
        return self._cache_arguments(values)

    @staticmethod
    def _cache_value_is_off(value: CacheValue | None) -> bool:
        return value is False or (isinstance(value, str) and value.strip().upper() in {"0", "OFF", "FALSE", "NO"})

    def _cached_generator(self, generated: GeneratedWorkspaceDirectory) -> str | None:
        """Read an actual existing build-tree generator without trusting request options."""
        try:
            cache = generated.read_text("CMakeCache.txt")
        except WorkspaceFileNotFoundError:
            return None
        except WorkspaceError:
            return None
        match = _CMAKE_GENERATOR_CACHE.search(cache)
        return match.group("value") if match is not None else None

    def _requested_generator(
        self,
        explicit_generator: str | None,
        preset: str | None,
        existing_generator: str | None,
        generated: GeneratedWorkspaceDirectory,
        kit: CMakeKit | None,
    ) -> str | None:
        """Resolve generator precedence without changing an existing build tree."""
        requested = self._validate_optional_name(explicit_generator, label="generator")
        if requested is not None:
            if preset is not None:
                raise CMakePresetKitConflictError(
                    "An explicit configure generator and a CMake preset are alternative generator workflows; choose one."
                )
                profile_id = self._profile_by_binary_dir.get(generated.relative_path)
                if profile_id is not None and profile_id in self._target_profiles:
                    profile = self._target_profiles[profile_id]
                    self._target_profiles[profile_id] = CachedCMakeTargetProfile(
                        profile_id=profile.profile_id,
                        targets=profile.targets,
                        observed_at=profile.observed_at,
                        expires_at=profile.expires_at,
                        kit_id=None if effective_kit is None else effective_kit.id,
                    )
            return requested
        if preset is not None:
            return None
        if self._config is not None and self._config.cmake_generator is not None:
            return self._config.cmake_generator
        if existing_generator is not None:
            return None
        if kit is not None and kit.preferred_generator is not None:
            return kit.preferred_generator
        if not generated.is_empty():
            return None
        if self._can_auto_select_ninja():
            return "Ninja"
        return None

    @classmethod
    def _generator_is_command_line(cls, generator: str | None) -> bool:
        return generator is not None and not generator.casefold().startswith("visual studio")

    @staticmethod
    def _cached_compiler_family(generated: GeneratedWorkspaceDirectory) -> str | None:
        try:
            cache = generated.read_text("CMakeCache.txt")
        except WorkspaceError:
            return None
        match = _CMAKE_COMPILER_CACHE.search(cache)
        if match is None:
            return None
        value = match.group("value").replace("\\", "/").casefold()
        name = value.rsplit("/", 1)[-1]
        if name in {"cl.exe", "cl"}:
            return "msvc"
        if "clang-cl" in name:
            return "clang-cl"
        if "clang" in name:
            return "clang"
        if name.startswith(("g++", "gcc")):
            return "gcc"
        return None

    @staticmethod
    def _build_tree_incompatibility_message(
        cached_generator: str | None,
        requested_generator: str | None,
        cached_family: str | None,
        requested_family: str | None,
        kit_id: str,
    ) -> str:
        fields = (
            f"cached generator family: {cached_generator or 'unknown'}",
            f"requested generator family: {requested_generator or 'unknown'}",
            f"cached compiler family: {cached_family or 'unknown'}",
            f"requested compiler family: {requested_family or 'unknown'}",
            f"suggested binary_dir: build/forgemcp/{kit_id}",
        )
        return "Existing build tree is incompatible; " + "; ".join(fields) + "."

    def _can_auto_select_ninja(self) -> bool:
        """Require a qualified Ninja and a compatible selected toolchain environment."""
        if self._toolchain is None or self._toolchain.executable("ninja") is None:
            return False
        if self._config is None:
            return True
        if self._config.toolchain == "msvc" and getattr(self._toolchain, "toolchain_environment", None) is None:
            return False
        return True

    def _preflight_generator(
        self, existing: str | None, requested: str | None, *, kit_id: str | None = None
    ) -> None:
        if existing is not None and requested is not None and existing != requested:
            raise CMakeBuildTreeIncompatibleError(
                self._build_tree_incompatibility_message(
                    existing, requested, self._cached_compiler_family_from_text(""), None,
                    kit_id or "separate-build",
                )
            )
        known = existing or requested
        if self._compile_commands_mode == "required" and known is not None and not self._generator_supports_compile_commands(known):
            raise CompilationDatabaseRequirementError(
                "compile_commands=required is unsupported by the selected CMake generator; choose an empty Ninja or Makefiles build directory."
            )

    @staticmethod
    def _generator_supports_compile_commands(generator: str) -> bool:
        """Recognize only CMake generator families with documented support."""
        return generator in {
            "Ninja", "Ninja Multi-Config", "Unix Makefiles", "MinGW Makefiles",
            "MSYS Makefiles", "NMake Makefiles", "NMake Makefiles JOM",
            "Borland Makefiles", "Watcom WMake",
        }

    @classmethod
    def _generator_is_single_config(cls, generator: str) -> bool:
        return cls._generator_supports_compile_commands(generator) and generator != "Ninja Multi-Config"

    def _actual_generator(self, generated: GeneratedWorkspaceDirectory) -> str | None:
        return self._cached_generator(generated)

    def _validate_compilation_database(self, generated: GeneratedWorkspaceDirectory) -> CompilationDatabaseStatus:
        """Validate only bounded metadata of CMake's generated database."""
        generator = self._actual_generator(generated)
        support = (
            "supported" if generator is not None and self._generator_supports_compile_commands(generator)
            else "unsupported" if generator is not None
            else "unknown"
        )
        if self._compile_commands_mode == "off":
            return CompilationDatabaseStatus(
                availability="off", generator_support=support, generator=generator,
                binary_dir=generated.relative_path,
            )
        if support == "unsupported":
            return CompilationDatabaseStatus(
                availability="unsupported", generator_support=support, generator=generator,
                binary_dir=generated.relative_path,
            )
        try:
            text, snapshot = generated.read_text_with_snapshot(
                "compile_commands.json", maximum_bytes=MAX_COMPILATION_DATABASE_BYTES
            )
            document = json.loads(text)
        except WorkspaceFileNotFoundError:
            return CompilationDatabaseStatus(availability="missing", generator_support=support, generator=generator, binary_dir=generated.relative_path)
        except (WorkspaceError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return CompilationDatabaseStatus(availability="invalid", generator_support=support, generator=generator, binary_dir=generated.relative_path)
        if (
            not isinstance(document, list)
            or len(document) > MAX_COMPILATION_DATABASE_ENTRIES
            or self._json_depth(document) > MAX_COMPILATION_DATABASE_DEPTH
            or not self._json_strings_within_limits(document)
        ):
            return CompilationDatabaseStatus(availability="invalid", generator_support=support, generator=generator, binary_dir=generated.relative_path)
        invalid = 0
        external = 0
        seen_entries: set[tuple[str, str]] = set()
        for entry in document:
            if not self._valid_database_entry(entry):
                invalid += 1
                continue
            assert isinstance(entry, Mapping)
            file_name = entry.get("file")
            assert isinstance(file_name, str)
            try:
                directory = entry.get("directory")
                assert isinstance(directory, str)
                base = self._workspace.validate_reported_path(directory)
                file_path = self._workspace.validate_reported_path(file_name, relative_to=base)
            except WorkspaceError:
                external += 1
                continue
            identity = (os.path.normcase(base), os.path.normcase(file_path))
            if identity in seen_entries:
                invalid += 1
                continue
            seen_entries.add(identity)
        return CompilationDatabaseStatus(
            availability="available" if invalid == 0 else "invalid",
            generator_support=support,
            generator=generator,
            binary_dir=generated.relative_path,
            entry_count=len(document),
            omitted_external_entries=external,
            invalid_entries=invalid,
            fingerprint=snapshot.sha256 if invalid == 0 else None,
        )

    @staticmethod
    def _json_depth(value: object, depth: int = 0) -> int:
        if depth > MAX_COMPILATION_DATABASE_DEPTH:
            return depth
        if isinstance(value, Mapping):
            return max((CMakeService._json_depth(item, depth + 1) for item in value.values()), default=depth)
        if isinstance(value, list):
            return max((CMakeService._json_depth(item, depth + 1) for item in value), default=depth)
        return depth

    @staticmethod
    def _json_strings_within_limits(value: object) -> bool:
        """Bound every decoded project-input string before inspecting entries."""
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, str):
                if len(current) > 65_536:
                    return False
            elif isinstance(current, Mapping):
                pending.extend(current.keys())
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)
        return True

    @staticmethod
    def _valid_database_entry(value: object) -> bool:
        if not isinstance(value, Mapping):
            return False
        file_name = value.get("file")
        directory = value.get("directory")
        command = value.get("command")
        arguments = value.get("arguments")
        if not CMakeService._safe_database_string(file_name, 4096):
            return False
        if not CMakeService._safe_database_string(directory, 4096):
            return False
        output = value.get("output")
        if output is not None and not CMakeService._safe_database_string(output, 4096):
            return False
        if command is not None and not CMakeService._safe_database_string(command, 65_536):
            return False
        if arguments is not None and not (
            isinstance(arguments, list)
            and bool(arguments)
            and len(arguments) <= 1024
            and all(CMakeService._safe_database_string(item, 16_384) for item in arguments)
        ):
            return False
        return command is not None or arguments is not None

    @staticmethod
    def _safe_database_string(value: object, maximum: int) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and len(value) <= maximum
            and "\x00" not in value
            and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        )

    def _compilation_warnings(self, database: CompilationDatabaseStatus) -> tuple[str, ...]:
        warnings: list[str] = []
        if database.availability in {"missing", "invalid", "unsupported"}:
            warnings.append("compile_commands_unavailable")
        if database.generator_support == "unsupported":
            warnings.append("compile_commands_generator_unsupported")
        return tuple(warnings)

    def _file_api_is_valid(self, generated: GeneratedWorkspaceDirectory) -> bool:
        """Validate the complete File API model without exposing its contents."""
        try:
            configurations = self._parse_target_configurations(
                self._load_codemodel(generated), generated
            )
        except Exception:
            # This is a post-process advisory check.  A malformed project
            # reply must become one fixed warning category, never a raw parser
            # error or an accidental transport failure after CMake succeeded.
            return False
        self._cache_target_profile(
            CMakeTargetList(binary_dir=generated.relative_path, configurations=configurations)
        )
        return True

    def _cache_target_profile(self, targets: CMakeTargetList) -> CachedCMakeTargetProfile:
        """Retain validated targets behind an opaque application-local identifier."""
        self._expire_target_profiles()
        existing = self._profile_by_binary_dir.get(targets.binary_dir)
        profile_id = existing if existing in self._target_profiles else secrets.token_urlsafe(18)
        profile = CachedCMakeTargetProfile(
            profile_id=profile_id,
            targets=targets,
            observed_at=datetime.now(UTC),
            expires_at=monotonic() + CACHED_DISCOVERY_PROFILE_TTL_SECONDS,
        )
        self._target_profiles[profile_id] = profile
        self._target_profiles.move_to_end(profile_id)
        self._profile_by_binary_dir[targets.binary_dir] = profile_id
        while len(self._target_profiles) > MAX_CACHED_DISCOVERY_PROFILES:
            removed_id, removed = self._target_profiles.popitem(last=False)
            if self._profile_by_binary_dir.get(removed.targets.binary_dir) == removed_id:
                del self._profile_by_binary_dir[removed.targets.binary_dir]
        return profile

    def _expire_target_profiles(self) -> None:
        now = monotonic()
        for profile_id in tuple(self._target_profiles):
            profile = self._target_profiles[profile_id]
            if profile.expires_at > now:
                continue
            del self._target_profiles[profile_id]
            if self._profile_by_binary_dir.get(profile.targets.binary_dir) == profile_id:
                del self._profile_by_binary_dir[profile.targets.binary_dir]

    def _configure_warnings(
        self, database: CompilationDatabaseStatus, *, file_api_valid: bool
    ) -> tuple[str, ...]:
        """Return only fixed success-with-warning categories for configure."""
        warnings = list(self._compilation_warnings(database))
        if not file_api_valid:
            warnings.append("file_api_unavailable")
        if self._configuration_stale:
            warnings.append("configuration_stale")
        if self._mutations is not None and self._mutations.degraded:
            warnings.append("workspace_mutation_delivery_degraded")
        return tuple(dict.fromkeys(warnings))

    def _status_warnings(self) -> tuple[str, ...]:
        warnings: list[str] = []
        if self._configuration_stale or bool(self._mutations and self._mutations.degraded):
            warnings.append("configuration_stale")
        if self._selected_kit_incompatible:
            warnings.append("selected_kit_incompatible")
        if self._mutations is not None and self._mutations.degraded:
            warnings.append("workspace_mutation_delivery_degraded")
        if self._compilation_database is not None:
            warnings.extend(self._compilation_warnings(self._compilation_database))
        return tuple(dict.fromkeys(warnings))

    def mark_workspace_mutation(self, paths: Sequence[str], *, generation: int = 0) -> None:
        """Mark a cached configuration stale for relevant committed source changes."""
        if self._configured_binary_dir is None or self._configured_source_dir is None:
            return
        for path in paths:
            lower = path.casefold()
            if lower == self._configured_binary_dir.casefold() or lower.startswith(
                f"{self._configured_binary_dir.casefold()}/"
            ):
                continue
            if (
                self._configured_toolchain_file is not None
                and lower == self._configured_toolchain_file.casefold()
            ):
                self._configuration_stale = True
                self._last_relevant_mutation_generation = max(
                    self._last_relevant_mutation_generation, generation
                )
                return
            source = self._configured_source_dir.casefold()
            if source != ".":
                if lower == source:
                    continue
                prefix = f"{source}/"
                if not lower.startswith(prefix):
                    continue
                lower = lower[len(prefix):]
            if (
                lower == "cmakelists.txt"
                or lower.endswith("/cmakelists.txt")
                or lower.endswith(".cmake")
                or lower in {"cmakepresets.json", "cmakeuserpresets.json"}
                or lower.endswith("/cmakepresets.json")
                or lower.endswith("/cmakeuserpresets.json")
            ):
                self._configuration_stale = True
                self._last_relevant_mutation_generation = max(
                    self._last_relevant_mutation_generation, generation
                )
                return

    def _refresh_configuration_staleness_since(self, generation: int) -> None:
        """Apply retained mutations after a configure generation capture."""
        self._configuration_stale = self._last_relevant_mutation_generation > generation
        if self._mutations is None:
            return
        batches = self._mutations.batches_since(generation)
        if batches is None:
            self._configuration_stale = True
            return
        for batch in batches:
            self.mark_workspace_mutation(
                tuple(change.path for change in batch.changes), generation=batch.generation
            )

    def _toolchain_file_from_request(
        self, values: Mapping[str, CacheValue] | None, source_dir: str
    ) -> str | None:
        if values is None:
            return None
        value = values.get("CMAKE_TOOLCHAIN_FILE")
        if not isinstance(value, str) or not value:
            return None
        try:
            return self._workspace.validate_reported_path(value, relative_to=source_dir)
        except WorkspaceError:
            return None

    @staticmethod
    def _validate_optional_name(value: str | None, *, label: str) -> str | None:
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 256
            or "\x00" in value
            or value.startswith("-")
            or any(ord(character) < 32 for character in value)
        ):
            raise CMakeRequestError(
                f"The {label} must be a bounded non-empty NUL-free name that does not start with '-'."
            )
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
