"""Transport-neutral immutable models owned by the CMake feature module."""

from __future__ import annotations

from pydantic import Field

from forgemcp.models import ProcessResult
from forgemcp.models._base import ForgeModel


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


class CMakeStatus(ForgeModel):
    """Combined environment status for the CMake feature."""

    available: bool = Field(description="Whether CMake and CTest are both available and CMake is supported.")
    minimum_cmake_version: CMakeVersion = Field(description="Lowest CMake version supported by this feature.")
    cmake: CMakeToolStatus = Field(description="CMake executable status.")
    ctest: CMakeToolStatus = Field(description="CTest executable status.")
    profile: CMakeResolvedProfile | None = Field(default=None, description="Resolved safe workspace CMake profile.")


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
