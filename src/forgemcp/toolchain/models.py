"""Path-free, transport-neutral C++ toolchain kit contracts.

The discovery service keeps executable paths and filtered environments private.
These models are deliberately suitable for MCP, status, resources, and local
operator output without turning a kit into a serialized VS Code configuration.
"""

from __future__ import annotations

from pydantic import Field

from forgemcp.models._base import ForgeModel


class CMakeKit(ForgeModel):
    """One immutable, qualified-or-explained C/C++ CMake toolchain choice."""

    id: str = Field(min_length=8, max_length=96, description="Stable opaque ForgeMCP kit identifier.")
    display_name: str = Field(min_length=1, max_length=256, description="Safe human-facing kit display name.")
    source: str = Field(min_length=1, description="explicit, visual_studio, standalone, or path.")
    compiler_family: str = Field(min_length=1, description="msvc, clang-cl, clang, gcc, or unknown.")
    c_compiler: str = Field(min_length=1, description="Compiler identity only; never an executable path.")
    cxx_compiler: str = Field(min_length=1, description="C++ compiler identity only; never an executable path.")
    compiler_version: str | None = Field(default=None, description="Safe parsed compiler version, when bounded probing confirmed it.")
    host_arch: str = Field(min_length=1, description="Qualified tool host architecture.")
    target_arch: str = Field(min_length=1, description="Qualified target architecture.")
    visual_studio_instance: str | None = Field(default=None, description="Safe Visual Studio instance identity; never an installation path.")
    visual_studio_version: str | None = Field(default=None, description="Visual Studio installation version, when applicable.")
    environment_profile: str = Field(min_length=1, description="none or filtered_visual_studio.")
    compatible_generators: tuple[str, ...] = Field(default=(), description="Qualified CMake generator names.")
    preferred_generator: str | None = Field(default=None, description="Preferred generator, never an unconditional override.")
    compile_commands: str = Field(min_length=1, description="supported, unavailable, or unknown.")
    debugger_compatibility: str = Field(min_length=1, description="compatible, unavailable, or incompatible.")
    readiness: str = Field(min_length=1, description="ready, degraded, or rejected.")
    reasons: tuple[str, ...] = Field(default=(), description="Fixed safe warning/rejection categories only.")


class CMakeKitList(ForgeModel):
    """Cached deterministic kit discovery state."""

    kits: tuple[CMakeKit, ...] = Field(default=(), description="Deterministically ordered cached kits.")
    discovery_state: str = Field(min_length=1, description="cached, unavailable, or degraded.")
    complete: bool = Field(description="Whether bounded discovery finished without a fixed degradation category.")


class CMakeKitSelection(ForgeModel):
    """Application-scoped CMake kit selection and monotonic generation."""

    selected_kit: str | None = Field(default=None, description="Explicitly selected opaque kit identifier.")
    effective_kit: CMakeKit | None = Field(default=None, description="Kit effective under selection precedence.")
    selection_generation: int = Field(ge=0, description="Monotonic application-local selection generation.")
    source: str = Field(min_length=1, description="runtime, cli, environment, automatic, or none.")
