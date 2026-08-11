"""Configurable, fail-closed limits and ignore rules for one workspace."""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatchcase


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    """Filesystem policy applied by :class:`WorkspaceService`.

    The default excludes VCS, virtual-environment, and common CMake build
    directories.  Callers may replace either ignore collection when composing
    a service for a workspace with different generated-directory conventions.
    """

    max_read_bytes: int = 1_048_576
    max_patch_bytes: int = 1_048_576
    ignored_directory_names: frozenset[str] = field(
        default_factory=lambda: frozenset({".git", ".venv", "build"})
    )
    ignored_directory_patterns: frozenset[str] = field(
        default_factory=lambda: frozenset({"build-*", "cmake-build-*"})
    )

    def __post_init__(self) -> None:
        """Reject nonsensical limits and ambiguous directory rules early."""
        if (
            not isinstance(self.max_read_bytes, int)
            or isinstance(self.max_read_bytes, bool)
            or self.max_read_bytes <= 0
        ):
            raise ValueError("max_read_bytes must be greater than zero.")
        if (
            not isinstance(self.max_patch_bytes, int)
            or isinstance(self.max_patch_bytes, bool)
            or self.max_patch_bytes <= 0
        ):
            raise ValueError("max_patch_bytes must be greater than zero.")
        names = frozenset(self.ignored_directory_names)
        patterns = frozenset(self.ignored_directory_patterns)
        for name in names:
            if not isinstance(name, str) or not name or "/" in name or "\\" in name:
                raise ValueError("Ignored directory names must be single non-empty path components.")
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern or "/" in pattern or "\\" in pattern:
                raise ValueError("Ignored directory patterns must be single non-empty path components.")
        object.__setattr__(self, "ignored_directory_names", names)
        object.__setattr__(self, "ignored_directory_patterns", patterns)

    def ignores_directory(self, name: str) -> bool:
        """Return whether one directory basename is excluded by this policy."""
        return name in self.ignored_directory_names or any(
            fnmatchcase(name, pattern) for pattern in self.ignored_directory_patterns
        )
