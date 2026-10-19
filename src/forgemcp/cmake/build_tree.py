"""Managed CMake build trees and direct test execution paths."""

import os
import shutil
from pathlib import Path

from forgemcp.workspace.path import WorkspacePath

from .errors import CMakeError


def clear_build_directory(
    directory: Path,
    root: Path,
    storage: Path,
    protected: set[Path],
) -> None:
    """Remove an owned tree only after root, protected subtree, and link checks succeed."""
    if (
        directory == root
        or not directory.resolve().is_relative_to(root)
        or storage.is_relative_to(directory)
        or any(path.is_relative_to(directory) for path in protected)
    ):
        raise CMakeError(
            "Build directory contains a protected path or escaped its root."
        )

    def raise_walk_error(error: OSError) -> None:
        """Reject incomplete pre-removal inspection rather than ignoring traversal failures."""
        raise error

    try:
        if not directory.exists():
            return
        for parent, directories, files in os.walk(
            directory,
            followlinks=False,
            onerror=raise_walk_error,
        ):
            for name in (*directories, *files):
                item = Path(parent) / name
                if item.is_symlink() or item.is_junction():
                    raise CMakeError("Build directory contains a link or junction.")
        if not directory.resolve().is_relative_to(root):
            raise CMakeError("Build directory escaped its root.")
        shutil.rmtree(directory)
    except OSError as error:
        raise CMakeError("Cannot clear build directory.") from error


def execution_path(reference: WorkspacePath, project: Path, storage: Path) -> Path:
    """Resolve a test executable or working directory without escaping its managed root."""
    if reference.area == "root":
        raise CMakeError("Test execution paths must be inside project/ or storage/.")
    base = project if reference.area == "project" else storage
    try:
        if os.name == "nt" and any(
            os.path.isreserved(part) for part in Path(reference.relative).parts
        ):
            raise CMakeError(f"Test execution path contains a reserved name: {reference}.")
        path = (base / reference.relative).resolve()
        if not path.is_relative_to(base):
            raise CMakeError(f"Test execution path resolves outside its root: {reference}.")
        return path
    except OSError as error:
        raise CMakeError(f"Cannot resolve test execution path: {reference}.") from error
