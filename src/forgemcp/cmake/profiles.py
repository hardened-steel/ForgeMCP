"""Operator-owned CMake profiles and CLI parsing."""

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from forgemcp.diagnostics import available_values, display_value
from forgemcp.toolchain.tools import cmake
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.spec import Toolset
from forgemcp.workspace.path import WorkspacePath

from .build_tree import build_path
from .errors import CMakeError
from .models import CMakeProfile


class ProfileDefinition(BaseModel):
    """Operator settings for a manual CMake profile or independently selected native presets."""

    name: str
    toolset: str = "system"
    configure_presets: list[str] | None = None
    build_presets: list[str] | None = None
    test_presets: list[str] | None = None
    configuration: str | None = None
    generator: str | None = None
    build_directory: WorkspacePath | None = None
    c_compiler: str | None = None
    cxx_compiler: str | None = None
    toolchain_file: WorkspacePath | None = None
    definitions: dict[str, str] = Field(default_factory=dict)

    @property
    def uses_presets(self) -> bool:
        """Report whether any preset selection was explicitly supplied."""
        return any(
            presets is not None
            for presets in (self.configure_presets, self.build_presets, self.test_presets)
        )


def parse_profiles(
    groups: Sequence[Sequence[str]],
    default_toolset: str = "system",
) -> tuple[ProfileDefinition, ...]:
    """Parse repeated NAME KEY=VALUE groups without reading environment variables."""
    if not default_toolset.strip():
        raise CMakeError("--cmake-toolset must not be empty.")
    profiles = []
    names = set()
    fields = {
        "toolset": "toolset",
        "configuration": "configuration",
        "generator": "generator",
        "build-directory": "build_directory",
        "c-compiler": "c_compiler",
        "cxx-compiler": "cxx_compiler",
        "toolchain-file": "toolchain_file",
    }
    for group in groups:
        if not group or not group[0].strip() or "=" in group[0]:
            raise CMakeError("--cmake-profile requires NAME [KEY=VALUE ...].")
        name = group[0]
        if name in names:
            raise CMakeError(f"Duplicate CMake profile {name!r}.")
        names.add(name)
        values = {"name": name, "toolset": default_toolset}
        definitions = {}
        seen = set()
        for assignment in group[1:]:
            key, separator, value = assignment.partition("=")
            if not separator or not value:
                raise CMakeError(f"Expected KEY=VALUE in profile {name!r}.")
            if key == "define":
                variable, equals, setting = value.partition("=")
                if not equals or not variable or variable in definitions:
                    raise CMakeError("define requires a unique CMAKE_VARIABLE=value.")
                definitions[variable] = setting
            elif key in ("configure-preset", "build-preset", "test-preset"):
                field = key.replace("-", "_") + "s"
                values.setdefault(field, []).append(value)
            elif key in fields and key not in seen:
                seen.add(key)
                values[fields[key]] = value
            else:
                raise CMakeError(f"Unknown or repeated profile setting {key!r}.")
        profile = ProfileDefinition(**values, definitions=definitions)
        if profile.uses_presets:
            if any(
                (
                    profile.generator,
                    profile.c_compiler,
                    profile.cxx_compiler,
                    profile.toolchain_file,
                    profile.definitions,
                )
            ):
                raise CMakeError(
                    "A preset profile takes configure settings from its preset; "
                    "do not also specify generator, compilers, toolchain or definitions."
                )
        profiles.append(profile)
    return tuple(profiles)


def require_configuration(
    cache: dict[str, str],
    configuration: str | None,
) -> None:
    """Validate the requested configuration against single- or multi-config cache settings."""
    choices = cache.get("CMAKE_CONFIGURATION_TYPES", "").split(";")
    if choices != [""] and configuration not in choices:
        raise CMakeError(
            f"Operator configuration={display_value(configuration)} is unavailable in this "
            "multi-config build directory. Correct --cmake-profile or its preset. "
            + available_values(
                choices,
                "workspace_read_file for the build directory's CMakeCache.txt",
            )
        )
    if (
        choices == [""]
        and configuration is not None
        and cache.get("CMAKE_BUILD_TYPE") != configuration
    ):
        raise CMakeError(
            f"Operator configuration={display_value(configuration)} differs from this build "
            f"directory's CMAKE_BUILD_TYPE={display_value(cache.get('CMAKE_BUILD_TYPE'))}. "
            "Run cmake_configure for this profile before building or testing."
        )


def validate_names(names: list[str] | None, parameter: str) -> None:
    """Require explicit target/test selections to contain nonempty names without NUL."""
    if names is None:
        return
    kind = "target name" if parameter == "targets" else "exact test name"
    defaults = "build defaults" if parameter == "targets" else "CTest defaults"
    if not names:
        raise CMakeError(
            f"{parameter}=[] is empty. Omit {parameter} or pass null to use {defaults}."
        )
    for index, name in enumerate(names):
        if not name or "\0" in name:
            raise CMakeError(
                f"{parameter}[{index}]={display_value(name)} must be a nonempty "
                f"{kind} without NUL. Names are project-specific literal strings."
            )


def operation_presets(profile: CMakeProfile, operation: str) -> list[str | None]:
    """Choose operation presets or a permitted ordinary build-directory invocation."""
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


def validate_presets(
    definition: ProfileDefinition,
    available: cmake.PresetsResult,
    toolset_id: str,
) -> None:
    """Validate operator preset selections against the already acquired CMake catalog."""
    for kind in ("configure", "build", "test"):
        chosen = getattr(definition, kind + "_presets") or []
        if len(chosen) != len(set(chosen)):
            repeated = next(name for index, name in enumerate(chosen) if name in chosen[:index])
            raise CMakeError(
                f"Repeated {kind} preset {display_value(repeated)} in operator profile "
                f"{display_value(definition.name)}. List each preset once in --cmake-profile."
            )
        unknown = set(chosen) - set(getattr(available, kind))
        if unknown:
            raise CMakeError(
                f"Unavailable {kind} presets {display_value(', '.join(sorted(unknown)))} in "
                f"operator profile {display_value(definition.name)} for toolset "
                f"{display_value(toolset_id)}. "
                + available_values(
                    getattr(available, kind),
                    "the selected CMake executable's --list-presets=all command",
                )
            )


def plain_profile(
    definition: ProfileDefinition,
    toolset_id: str,
    project: Path,
    storage: Path,
    protected: set[Path],
) -> CMakeProfile:
    """Resolve manual profile defaults and require a safe managed build directory."""
    directory = definition.build_directory
    if directory is None:
        if (
            not definition.name
            or definition.name in (".", "..")
            or any(char in definition.name for char in "/\\\0")
        ):
            raise CMakeError(
                f"Operator profile name {display_value(definition.name)} cannot form a "
                "default build directory. Use one nonempty directory name without slash, "
                "backslash, NUL, '.' or '..', or set build-directory explicitly."
            )
        directory = WorkspacePath(f"storage/build/cmake-{definition.name}")
    build_path(directory, project, storage, protected)
    return CMakeProfile(
        name=definition.name,
        toolset_id=toolset_id,
        build_directory=directory,
        configuration=definition.configuration,
        generator=definition.generator or "Ninja",
    )


def configure_definitions(
    definition: ProfileDefinition,
    toolset: Toolset,
    toolchains: ToolchainService,
    project: Path,
    storage: Path,
) -> dict[str, str]:
    """Resolve compiler and toolchain settings and reject conflicting cache overrides."""
    result = dict(definition.definitions)
    reserved = {"CMAKE_HOME_DIRECTORY", "CMAKE_CACHEFILE_DIR"}
    if reserved.intersection(key.partition(":")[0] for key in result):
        raise CMakeError("CMake source/build directories cannot be cache overrides.")
    for field, variable in (
        (definition.c_compiler, "CMAKE_C_COMPILER"),
        (definition.cxx_compiler, "CMAKE_CXX_COMPILER"),
    ):
        if field is not None:
            tool = toolchains.get_tool(toolset.id, field)
            if tool is None:
                raise CMakeError(
                    f"Operator compiler setting {display_value(field)} in profile "
                    f"{display_value(definition.name)} is unavailable in toolset "
                    f"{display_value(toolset.id)}. "
                    f"Call toolset_get(toolset_id={display_value(toolset.id)}) to inspect "
                    "its tools; the operator must correct the compiler setting."
                )
            if any(key.partition(":")[0] == variable for key in result):
                raise CMakeError(f"Duplicate setting for {variable}.")
            result[variable] = str(tool.path)
    if definition.toolchain_file is not None:
        if any(key.partition(":")[0] == "CMAKE_TOOLCHAIN_FILE" for key in result):
            raise CMakeError("Duplicate setting for CMAKE_TOOLCHAIN_FILE.")
        reference = definition.toolchain_file
        if reference.area == "root":
            raise CMakeError("Toolchain file must be inside the project or storage root.")
        base = project if reference.area == "project" else storage
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
            raise CMakeError(
                f"Operator profile {display_value(definition.name)} has "
                f"configuration={display_value(definition.configuration)} conflicting "
                f"with CMAKE_BUILD_TYPE={display_value(configured)}. "
                "Set these values consistently in --cmake-profile."
            )
        if not configured:
            result["CMAKE_BUILD_TYPE"] = definition.configuration
    if not any(key.partition(":")[0] == "CMAKE_EXPORT_COMPILE_COMMANDS" for key in result):
        result["CMAKE_EXPORT_COMPILE_COMMANDS"] = "ON"
    if (definition.generator or "Ninja") in ("Ninja", "Ninja Multi-Config"):
        ninja = toolchains.get_tool(toolset.id, "ninja")
        if ninja is None:
            raise CMakeError(
                f"Toolset {display_value(toolset.id)} has no ninja executable required "
                f"by generator {display_value(definition.generator or 'Ninja')} in "
                f"profile {display_value(definition.name)}. Inspect "
                f"toolset_get(toolset_id={display_value(toolset.id)}); "
                "the operator must install Ninja or correct the profile's generator/toolset."
            )
        if not any(key.partition(":")[0] == "CMAKE_MAKE_PROGRAM" for key in result):
            result["CMAKE_MAKE_PROGRAM"] = str(ninja.path)
    return result
