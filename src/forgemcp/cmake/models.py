"""CMake profiles and typed execution results shared with analysis and presentation."""

from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

from forgemcp.toolchain.tools import ctest
from forgemcp.workspace.path import WorkspacePath


class CMakeProfile(BaseModel):
    """A resolved operator profile with toolset, build directory, and preset selections."""

    name: str
    toolset_id: str
    mode: Literal["plain", "presets"] = "plain"
    build_directory: WorkspacePath | None = None
    configure_presets: list[str] = Field(default_factory=list)
    build_presets: list[str] = Field(default_factory=list)
    test_presets: list[str] = Field(default_factory=list)
    configuration: str | None = None
    generator: str | None = None


    @property
    def compilation_context(self) -> "CompilationContext | None":
        """Derive a candidate identity only for an unambiguous managed build directory."""
        if self.build_directory is None or len(self.configure_presets) > 1:
            return None
        identifier = quote(self.name, safe="")
        if self.configure_presets:
            identifier += "/" + quote(self.configure_presets[0], safe="")
        return CompilationContext(
            id=identifier,
            toolset_id=self.toolset_id,
            build_directory=self.build_directory,
            compilation_database=WorkspacePath(f"{self.build_directory}/compile_commands.json"),
        )


class CompilationContext(BaseModel):
    """A successful build configuration with its toolset and compilation database."""

    id: str
    toolset_id: str
    build_directory: WorkspacePath
    compilation_database: WorkspacePath


class CMakeConfigureResult(BaseModel):
    """One configure outcome with discovered paths, failure explanation, and process reference."""

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
    """One build outcome with progress counts, failure explanation, and process reference."""

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
    """One test outcome with parsed cases, failure explanation, and process reference."""

    profile: str
    preset: str | None = None
    configuration: str | None = None
    tests: list[ctest.TestCase] = Field(default_factory=list)
    error: str | None = Field(
        default=None,
        description="Null on success; failure explanation otherwise.",
    )
    process_id: int | None = None


class CMakeRunTestResult(BaseModel):
    """One direct executable run with its command inputs, error, and process reference."""

    executable: WorkspacePath
    arguments: list[str] = Field(default_factory=list)
    cwd: WorkspacePath
    error: str | None = Field(
        default=None,
        description="Null on success; failure explanation otherwise.",
    )
    process_id: int | None = None
