"""Operator-owned CMake profiles and CLI parsing."""

from collections.abc import Sequence

from pydantic import BaseModel, Field

from forgemcp.workspace.path import WorkspacePath

from .errors import CMakeError


class ProfileDefinition(BaseModel):
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
