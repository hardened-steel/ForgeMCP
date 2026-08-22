"""Transport-neutral immutable models owned by the CMake feature module."""

from __future__ import annotations

from pydantic import Field

from forgemcp.models import ProcessResult
from forgemcp.models._base import ForgeModel
from forgemcp.toolchain.models import CMakeKit, CMakeKitSelection


class CMakeVersion(ForgeModel):
    """A structured semantic CMake or CTest version parsed from its banner."""

    major: int = Field(ge=0, description="Major version component.")
    minor: int = Field(ge=0, description="Minor version component.")
    patch: int = Field(ge=0, description="Patch version component.")
    full: str = Field(min_length=1, description="Version text reported by the executable.")

    def at_least(self, other: "CMakeVersion") -> bool:
        """Return whether this version meets another semantic version floor."""
        return (self.major, self.minor, self.patch) >= (other.major, other.minor, other.patch)


class CMakeToolStatus(ForgeModel):
    """Availability and parsed version of one required executable."""

    executable: str = Field(min_length=1, description="Stable executable name requested by ForgeMCP.")
    available: bool = Field(description="Whether the executable returned a parseable successful version.")
    version: CMakeVersion | None = Field(default=None, description="Parsed version when available.")
    supported: bool = Field(description="Whether the version meets ForgeMCP's minimum floor.")
    error: str | None = Field(default=None, description="Intentional safe error when unavailable.")


class CMakeResolvedProfile(ForgeModel):
    """Workspace-relative effective CMake profile with safe provenance only."""

    source_dir: str = Field(min_length=1, description="Resolved workspace-relative CMake source directory.")
    binary_dir: str = Field(min_length=1, description="Resolved workspace-relative build directory.")
    source_dir_source: str = Field(min_length=1, description="Safe source category for source_dir.")
    binary_dir_source: str = Field(min_length=1, description="Safe source category for binary_dir.")
    configure_preset_source: str = Field(min_length=1, description="Safe source category for selected preset.")


class CompilationDatabaseStatus(ForgeModel):
    """Validated metadata for a generated compile_commands.json file only."""

    availability: str = Field(min_length=1, description="available, missing, invalid, unsupported, or off.")
    generator_support: str = Field(min_length=1, description="supported, unsupported, or unknown based on the actual CMake generator.")
    generator: str | None = Field(default=None, description="Actual bounded CMake generator name when safely observed.")
    binary_dir: str | None = Field(default=None, description="Workspace-relative generated build directory when available.")
    entry_count: int = Field(default=0, ge=0, description="Validated total database entries.")
    omitted_external_entries: int = Field(default=0, ge=0, description="External source entries omitted from ForgeMCP accounting.")
    invalid_entries: int = Field(default=0, ge=0, description="Entries rejected by bounded database validation.")
    fingerprint: str | None = Field(default=None, description="Content fingerprint; compiler commands and database contents are never returned.")


class CMakeStatus(ForgeModel):
    """Combined environment status for the CMake feature."""

    available: bool = Field(description="Whether CMake and CTest are both available and CMake is supported.")
    minimum_cmake_version: CMakeVersion = Field(description="Lowest CMake version supported by this feature.")
    cmake: CMakeToolStatus = Field(description="CMake executable status.")
    ctest: CMakeToolStatus = Field(description="CTest executable status.")
    profile: CMakeResolvedProfile | None = Field(default=None, description="Resolved safe workspace CMake profile.")
    compilation_database: CompilationDatabaseStatus | None = Field(default=None, description="Cached validated compilation-database metadata.")
    kit_selection: CMakeKitSelection | None = Field(default=None, description="Cached application-scoped kit selection and effective kit.")
    selected_kit_compatibility: str = Field(default="unknown", description="Compatible, stale, incompatible, or unknown relative to the cached build profile.")
    warnings: tuple[str, ...] = Field(default=(), description="Bounded safe warnings such as configuration_stale.")


class CMakeConfigurePreset(ForgeModel):
    """Safe subset of a configure preset, omitting environment and cache values."""

    name: str = Field(min_length=1, description="CMake configure preset name.")
    source_file: str = Field(min_length=1, description="Workspace-relative preset document path.")
    display_name: str | None = Field(default=None, description="Optional human-facing preset label.")
    description: str | None = Field(default=None, description="Optional preset description.")
    hidden: bool = Field(default=False, description="Whether CMake marks the preset hidden.")
    generator: str | None = Field(default=None, description="Configured generator name, if declared directly.")


class CMakeBuildPreset(ForgeModel):
    """Safe subset of a build preset, omitting environment and cache values."""

    name: str = Field(min_length=1, description="CMake build preset name.")
    source_file: str = Field(min_length=1, description="Workspace-relative preset document path.")
    display_name: str | None = Field(default=None, description="Optional human-facing preset label.")
    description: str | None = Field(default=None, description="Optional preset description.")
    hidden: bool = Field(default=False, description="Whether CMake marks the preset hidden.")
    configure_preset: str | None = Field(default=None, description="Referenced configure preset, if declared.")
    configuration: str | None = Field(default=None, description="Multi-config build configuration, if declared.")
    targets: tuple[str, ...] = Field(default=(), description="Directly declared target names only.")


class CMakeTestPreset(ForgeModel):
    """Safe subset of a test preset, omitting environment and cache values."""

    name: str = Field(min_length=1, description="CTest preset name.")
    source_file: str = Field(min_length=1, description="Workspace-relative preset document path.")
    display_name: str | None = Field(default=None, description="Optional human-facing preset label.")
    description: str | None = Field(default=None, description="Optional preset description.")
    hidden: bool = Field(default=False, description="Whether CMake marks the preset hidden.")
    configure_preset: str | None = Field(default=None, description="Referenced configure preset, if declared.")
    configuration: str | None = Field(default=None, description="Multi-config test configuration, if declared.")


class CMakePresetList(ForgeModel):
    """Detected safe preset summaries from project and user preset documents."""

    source_dir: str = Field(min_length=1, description="Workspace-relative source directory inspected.")
    preset_files: tuple[str, ...] = Field(description="Present preset document paths in CMake precedence order.")
    configure_presets: tuple[CMakeConfigurePreset, ...] = Field(default=())
    build_presets: tuple[CMakeBuildPreset, ...] = Field(default=())
    test_presets: tuple[CMakeTestPreset, ...] = Field(default=())


class CMakeConfigureResult(ForgeModel):
    """Result of one CMake configure command and its bounded process captures."""

    source_dir: str = Field(min_length=1, description="Validated workspace-relative source directory.")
    binary_dir: str = Field(min_length=1, description="Validated workspace-relative generated build directory.")
    preset: str | None = Field(default=None, description="Selected configure preset, if any.")
    process: ProcessResult = Field(description="Structured configure command result.")
    compilation_database: CompilationDatabaseStatus | None = Field(default=None, description="Validated generated compilation-database metadata after configure.")
    effective_kit: CMakeKit | None = Field(default=None, description="Effective path-free kit used for this configure operation.")
    generator: str | None = Field(default=None, description="Effective CMake generator when safely known.")
    compiler_family: str | None = Field(default=None, description="Effective compiler family when a ForgeMCP kit owns it.")
    diagnostic_category: str | None = Field(default=None, description="Fixed safe configure result category when a failure was classified.")
    diagnostics: tuple["CMakeDiagnostic", ...] = Field(default=(), description="Bounded sanitized configure diagnostics.")
    warnings: tuple[str, ...] = Field(default=(), description="Bounded safe configure warnings.")


class CMakeTargetMetadata(ForgeModel):
    """One target read from CMake File API codemodel v2 metadata."""

    name: str = Field(min_length=1, description="CMake target name.")
    target_id: str = Field(min_length=1, description="Stable CMake File API target identifier.")
    type: str = Field(min_length=1, description="CMake target type such as EXECUTABLE or STATIC_LIBRARY.")
    build_directory: str = Field(min_length=1, description="Workspace-relative target build directory.")
    artifacts: tuple[str, ...] = Field(default=(), description="Workspace-relative artifact paths.")
    sources: tuple[str, ...] = Field(default=(), description="Workspace-relative source paths.")
    dependencies: tuple[str, ...] = Field(default=(), description="Dependent target names or IDs.")


class CMakeConfigurationTargets(ForgeModel):
    """Target metadata for one CMake generator configuration."""

    name: str = Field(description="Configuration name; empty for single-config generators.")
    targets: tuple[CMakeTargetMetadata, ...] = Field(default=())


class CMakeTargetList(ForgeModel):
    """All configurations and targets supplied by one codemodel v2 reply."""

    binary_dir: str = Field(min_length=1, description="Generated build directory that owns the File API reply.")
    configurations: tuple[CMakeConfigurationTargets, ...] = Field(default=())


class CMakeBuildResult(ForgeModel):
    """Result of a whole-project or named-target CMake build."""

    binary_dir: str = Field(min_length=1, description="Validated workspace-relative build directory.")
    targets: tuple[str, ...] = Field(default=(), description="Requested targets; empty means the default build.")
    configuration: str | None = Field(default=None, description="Requested multi-config configuration, if any.")
    process: ProcessResult = Field(description="Structured build command result, including non-zero exits.")
    outcome: str = Field(default="unknown", description="success, success_with_warnings, compile_failure, linker_failure, build_system_failure, timeout, or cancelled.")
    diagnostics: tuple["CMakeDiagnostic", ...] = Field(default=(), description="Bounded sanitized compiler/linker diagnostics.")
    omitted_external_diagnostics: int = Field(default=0, ge=0, description="External diagnostic locations omitted.")
    invalid_diagnostics: int = Field(default=0, ge=0, description="Malformed or unsafe diagnostic records omitted.")
    complete: bool = Field(default=True, description="Whether capture and bounded parsing were complete.")


class CMakeDiagnostic(ForgeModel):
    """Path-safe compiler, linker, or configure diagnostic."""

    category: str = Field(min_length=1, description="configure, compiler, linker, or build_system.")
    severity: str = Field(min_length=1, description="error, warning, or information.")
    message: str = Field(min_length=1, max_length=1024, description="Bounded sanitized message without source/caret text or host paths.")
    code: str | None = Field(default=None, max_length=128, description="Safe compiler/linker code when present.")
    file: str | None = Field(default=None, max_length=4096, description="Workspace-relative file when proven.")
    line: int | None = Field(default=None, ge=0, description="Zero-based source line when proven.")
    column: int | None = Field(default=None, ge=0, description="Zero-based source column when proven.")


class CMakeBuildTree(ForgeModel):
    """Read-only summary of a bounded discovered existing CMake build tree."""

    profile_id: str = Field(min_length=8, max_length=96, description="Opaque deterministic existing-tree profile identifier.")
    binary_dir: str = Field(min_length=1, description="Workspace-relative binary directory.")
    source_matches_workspace: bool = Field(description="Whether CMakeCache source metadata resolves to the requested workspace source directory.")
    generator: str | None = Field(default=None, description="Cached CMake generator if safely read.")
    compiler_family: str | None = Field(default=None, description="Compiler family only when cache metadata safely confirms it.")
    compiler_version: str | None = Field(default=None, description="Safe compiler version when confirmed.")
    configuration_type: str = Field(min_length=1, description="single_config, multi_config, or unknown.")
    compilation_database: CompilationDatabaseStatus | None = Field(default=None, description="Validated compile_commands metadata.")
    stale: bool = Field(description="Whether known configuration state may be stale.")
    selected_kit_compatibility: str = Field(min_length=1, description="compatible, incompatible, stale, or unknown.")
    category: str = Field(min_length=1, description="adoptable, buildable, incompatible, or rejected.")


class CMakeBuildTreeList(ForgeModel):
    """Bounded read-only existing CMake build-tree discovery result."""

    build_trees: tuple[CMakeBuildTree, ...] = Field(default=(), description="Conventional workspace build trees discovered without running CMake.")


class CTestTest(ForgeModel):
    """One discovered CTest test name from ``--show-only=json-v1``."""

    name: str = Field(min_length=1, description="Exact CTest test name.")


class CTestTestList(ForgeModel):
    """Tests listed by CTest's documented JSON protocol."""

    binary_dir: str = Field(min_length=1, description="Validated workspace-relative build directory.")
    tests: tuple[CTestTest, ...] = Field(default=())
    process: ProcessResult = Field(description="Structured CTest listing command result.")


class CTestRunResult(ForgeModel):
    """Result of running all tests or an exact-name subset through CTest."""

    binary_dir: str = Field(min_length=1, description="Validated workspace-relative build directory.")
    test_names: tuple[str, ...] = Field(default=(), description="Exact selected test names; empty means all tests.")
    configuration: str | None = Field(default=None, description="Requested multi-config configuration, if any.")
    failed_tests: tuple[str, ...] = Field(default=(), description="Failed test names parsed from CTest's bounded output.")
    process: ProcessResult = Field(description="Structured CTest run result, including non-zero exits.")
