"""Fail-closed, immutable policy for external tool execution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from forgemcp.models import MAX_PROCESS_OUTPUT_CHARACTERS


def _normalise_relative_directory(value: str) -> str:
    """Validate a portable workspace-relative directory policy entry."""
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("Allowed working directories must be NUL-free strings.")
    native = Path(value)
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if (
        native.is_absolute()
        or bool(native.anchor)
        or bool(windows.drive)
        or bool(windows.root)
        or windows.is_absolute()
        or posix.is_absolute()
    ):
        raise ValueError("Allowed working directories must be relative to the workspace.")
    parts = tuple(part for part in native.parts if part not in {"", "."})
    if any(part == ".." for part in parts):
        raise ValueError("Allowed working directories must not contain parent traversal.")
    return "." if not parts else Path(*parts).as_posix()


def _validate_positive_seconds(value: float, name: str) -> float:
    """Validate one finite, positive duration without accepting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return float(value)


@dataclass(frozen=True, slots=True)
class ProcessPolicy:
    """Limits and explicit allow-lists for one :class:`ProcessRuntime`.

    The default admits only the conventional CMake, CTest, and clangd program
    names resolved from the environment present when the runtime is composed.
    DAP adapters and test executables need an explicit name or absolute-path
    entry.  Environment overrides are denied by default; a module must name
    the exact override keys it needs.  ``None`` deliberately opts a trusted
    in-process adapter into unrestricted overrides.
    """

    allowed_executables: frozenset[str] = field(
        default_factory=lambda: frozenset({"cmake", "ctest", "clangd"})
    )
    allowed_executable_paths: frozenset[Path] = field(default_factory=frozenset)
    allowed_working_directories: frozenset[str] | None = None
    default_timeout_seconds: float = 300.0
    maximum_timeout_seconds: float = 900.0
    max_output_characters: int = MAX_PROCESS_OUTPUT_CHARACTERS
    termination_grace_seconds: float = 5.0
    stream_close_timeout_seconds: float = 1.0
    allow_environment_inheritance: bool = True
    allowed_environment_overrides: frozenset[str] | None = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        """Reject ambiguous or unsafe policy values at composition time."""
        executable_names = frozenset(self.allowed_executables)
        for name in executable_names:
            if (
                not isinstance(name, str)
                or not name
                or "\x00" in name
                or "/" in name
                or "\\" in name
            ):
                raise ValueError("Allowed executable names must be bare, NUL-free program names.")

        raw_executable_paths = tuple(Path(path) for path in self.allowed_executable_paths)
        for path in raw_executable_paths:
            if not path.is_absolute():
                raise ValueError("Allowed executable paths must be absolute paths.")
        executable_paths = frozenset(path.resolve() for path in raw_executable_paths)

        working_directories = self.allowed_working_directories
        if working_directories is not None:
            working_directories = frozenset(
                _normalise_relative_directory(value) for value in working_directories
            )

        allowed_environment_overrides = self.allowed_environment_overrides
        if allowed_environment_overrides is not None:
            allowed_environment_overrides = frozenset(allowed_environment_overrides)
            for key in allowed_environment_overrides:
                if not isinstance(key, str) or not key or "\x00" in key or "=" in key:
                    raise ValueError("Allowed environment keys must be non-empty NUL-free names.")

        default_timeout = _validate_positive_seconds(
            self.default_timeout_seconds, "default_timeout_seconds"
        )
        maximum_timeout = _validate_positive_seconds(
            self.maximum_timeout_seconds, "maximum_timeout_seconds"
        )
        if default_timeout > maximum_timeout:
            raise ValueError("default_timeout_seconds must not exceed maximum_timeout_seconds.")
        grace_period = _validate_positive_seconds(
            self.termination_grace_seconds, "termination_grace_seconds"
        )
        stream_close_timeout = _validate_positive_seconds(
            self.stream_close_timeout_seconds, "stream_close_timeout_seconds"
        )
        if (
            not isinstance(self.max_output_characters, int)
            or isinstance(self.max_output_characters, bool)
            or not 0 < self.max_output_characters <= MAX_PROCESS_OUTPUT_CHARACTERS
        ):
            raise ValueError(
                "max_output_characters must be between one and MAX_PROCESS_OUTPUT_CHARACTERS."
            )
        if not isinstance(self.allow_environment_inheritance, bool):
            raise ValueError("allow_environment_inheritance must be a boolean.")

        object.__setattr__(self, "allowed_executables", executable_names)
        object.__setattr__(self, "allowed_executable_paths", executable_paths)
        object.__setattr__(self, "allowed_working_directories", working_directories)
        object.__setattr__(self, "allowed_environment_overrides", allowed_environment_overrides)
        object.__setattr__(self, "default_timeout_seconds", default_timeout)
        object.__setattr__(self, "maximum_timeout_seconds", maximum_timeout)
        object.__setattr__(self, "termination_grace_seconds", grace_period)
        object.__setattr__(self, "stream_close_timeout_seconds", stream_close_timeout)

    def allows_executable_name(self, name: str) -> bool:
        """Return whether a bare executable name is in this policy's allow-list."""
        if os.name == "nt":
            candidate = Path(name).stem.casefold()
            return any(Path(allowed).stem.casefold() == candidate for allowed in self.allowed_executables)
        return name in self.allowed_executables

    def allows_working_directory(self, relative_directory: str) -> bool:
        """Return whether one normalised workspace-relative directory is allowed."""
        return (
            self.allowed_working_directories is None
            or relative_directory in self.allowed_working_directories
        )
