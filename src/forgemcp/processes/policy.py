"""Fail-closed, immutable policy for external tool execution."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from forgemcp.models import MAX_PROCESS_OUTPUT_CHARACTERS


def _path_key(path: Path) -> str:
    """Return the filesystem comparison key used for approved executable paths."""
    value = str(path)
    return value.casefold() if os.name == "nt" else value


def _is_reparse_point(path: Path) -> bool:
    """Return whether a path entry is a Windows reparse point without following it."""
    try:
        attributes = path.lstat().st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _contains_link_or_reparse_point(path: Path) -> bool:
    """Reject a link/reparse point in the executable path rather than resolving through it."""
    current = path
    while True:
        if current.is_symlink() or _is_reparse_point(current):
            return True
        if current == current.parent:
            return False
        current = current.parent


@dataclass(frozen=True, slots=True)
class _ExecutableApproval:
    """Metadata captured for one exact executable approval at composition time."""

    path: Path
    device: int
    inode: int
    size: int
    modified_nanoseconds: int

    @classmethod
    def create(cls, raw_path: Path) -> "_ExecutableApproval":
        if "\x00" in str(raw_path):
            raise ValueError("Allowed executable paths must be NUL-free.")
        if not raw_path.is_absolute():
            raise ValueError("Allowed executable paths must be absolute paths.")
        if _contains_link_or_reparse_point(raw_path):
            raise ValueError("Allowed executable paths must not traverse symlinks or reparse points.")
        try:
            resolved = raw_path.resolve(strict=True)
            metadata = resolved.stat()
        except (FileNotFoundError, OSError) as error:
            raise ValueError("Allowed executable paths must exist.") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Allowed executable paths must name regular files.")
        if os.name != "nt" and not os.access(resolved, os.X_OK):
            raise ValueError("Allowed executable paths must be executable regular files.")
        return cls(
            path=resolved,
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            modified_nanoseconds=metadata.st_mtime_ns,
        )

    def still_matches(self, path: Path) -> bool:
        """Return whether an approved executable still names the approved file metadata."""
        if _path_key(path) != _path_key(self.path) or _contains_link_or_reparse_point(path):
            return False
        try:
            metadata = path.stat()
        except (FileNotFoundError, OSError):
            return False
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_dev == self.device
            and metadata.st_ino == self.inode
            and metadata.st_size == self.size
            and metadata.st_mtime_ns == self.modified_nanoseconds
        )


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
        default_factory=lambda: frozenset({"cmake", "ctest", "clangd", "clang-format", "clang-tidy"})
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
    _executable_approvals: tuple[_ExecutableApproval, ...] = field(
        init=False, repr=False, compare=False
    )

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
        approvals = tuple(_ExecutableApproval.create(path) for path in raw_executable_paths)
        executable_paths = frozenset(approval.path for approval in approvals)

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
        object.__setattr__(self, "_executable_approvals", approvals)
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

    def approves_exact_executable(self, path: Path) -> bool:
        """Return whether a current path still matches a concrete approved executable.

        An exact approval rejects symlink/reparse traversal and compares the
        canonical path case-insensitively on Windows.  The metadata captured
        at policy construction makes replacement after approval detectable.
        """
        return any(approval.still_matches(path) for approval in self._executable_approvals)

    def allows_working_directory(self, relative_directory: str) -> bool:
        """Return whether one normalised workspace-relative directory is allowed."""
        return (
            self.allowed_working_directories is None
            or relative_directory in self.allowed_working_directories
        )
