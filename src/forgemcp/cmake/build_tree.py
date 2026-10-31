"""Managed CMake build trees and direct test execution paths."""

import os
import shutil
from pathlib import Path

from forgemcp.diagnostics import display_value, os_error_reason
from forgemcp.workspace.path import WorkspacePath
from forgemcp.toolchain.tools import cmake

from .errors import CMakeError
from .models import CMakeProfile


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
                    relative = item.relative_to(root).as_posix()
                    raise CMakeError(
                        f"Cannot clear build directory: entry {display_value(relative)} "
                        "is a symbolic link or junction. Choose a build tree without links."
                    )
        if not directory.resolve().is_relative_to(root):
            raise CMakeError("Build directory escaped its root.")
        shutil.rmtree(directory)
    except OSError as error:
        raise CMakeError(
            f"Cannot clear build directory: {os_error_reason(error)}. "
            "Cleanup may be partial; inspect the configured build directory before retrying."
        ) from error


def execution_path(
    reference: WorkspacePath,
    project: Path,
    storage: Path,
    *,
    parameter: str = "path",
) -> Path:
    """Resolve a test executable or working directory without escaping its managed root."""
    if reference.area == "root":
        raise CMakeError(
            f"{parameter}={display_value(str(reference))} must use project/ or storage/. "
            "External root/ paths cannot be used for test execution."
        )
    base = project if reference.area == "project" else storage
    try:
        if os.name == "nt" and any(
            os.path.isreserved(part) for part in Path(reference.relative).parts
        ):
            raise CMakeError(
                f"{parameter}={display_value(str(reference))} contains a Windows reserved name."
            )
        path = (base / reference.relative).resolve()
        if not path.is_relative_to(base):
            raise CMakeError(
                f"{parameter}={display_value(str(reference))} resolves outside its root. "
                "Choose a path whose resolved target stays inside the selected root."
            )
        return path
    except OSError as error:
        raise CMakeError(
            f"Cannot resolve {parameter}={display_value(str(reference))}: "
            f"{os_error_reason(error)}."
        ) from error


def build_path(
    directory: WorkspacePath,
    project: Path,
    storage: Path,
    protected: set[Path],
) -> Path:
    """Validate a managed build path against root, link, and protected-path restrictions."""
    if directory.area == "root":
        raise CMakeError(
            f"build-directory={display_value(str(directory))} must use project/ or storage/."
        )
    base = project if directory.area == "project" else storage
    path = base / directory.relative
    try:
        if not path.resolve().is_relative_to(base):
            raise CMakeError(f"Build path resolves outside its root: {directory}.")
        if path in (project, storage):
            raise CMakeError(
                f"build-directory={display_value(str(directory))} is a workspace root. "
                "Choose a subdirectory such as storage/build/debug."
            )
        if os.name == "nt" and any(
            os.path.isreserved(part) for part in Path(directory.relative).parts
        ):
            raise CMakeError(f"Build path contains a reserved name: {directory}.")
        current = path
        while True:
            if current.is_symlink() or current.is_junction():
                raise CMakeError(
                    f"build-directory={display_value(str(directory))} traverses a symbolic "
                    "link or junction. Choose a build directory without links."
                )
            if current == base:
                break
            current = current.parent
        if any(path.is_relative_to(protected_path) for protected_path in protected):
            raise CMakeError(f"Build path is protected: {directory}.")
    except OSError as error:
        raise CMakeError(
            f"Cannot inspect build-directory={display_value(str(directory))}: "
            f"{os_error_reason(error)}."
        ) from error
    return path


def read_cache(
    directory: WorkspacePath,
    project: Path,
    storage: Path,
    protected: set[Path],
) -> dict[str, str]:
    """Read and parse a managed build directory's UTF-8 CMake cache."""
    path = build_path(directory, project, storage, protected) / "CMakeCache.txt"
    base = project if directory.area == "project" else storage
    try:
        if not path.resolve().is_relative_to(base):
            raise CMakeError("CMake cache resolves outside the build root.")
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CMakeError(
            f"Cannot read {display_value(str(directory) + '/CMakeCache.txt')}: "
            f"{os_error_reason(error)}."
        ) from error
    except UnicodeError as error:
        raise CMakeError(
            f"CMake cache in {display_value(str(directory))} is not valid UTF-8 text."
        ) from error
    if "\0" in text:
        raise CMakeError(f"CMake cache in {directory} is not a text file.")
    return cmake.parse_cache(text)


def require_configured(
    profile: CMakeProfile,
    project: Path,
    storage: Path,
    protected: set[Path],
) -> dict[str, str]:
    """Require a readable cache whose source directory belongs to this project."""
    if profile.build_directory is None:
        raise CMakeError("Set build-directory for operations without a preset.")
    try:
        cache = read_cache(profile.build_directory, project, storage, protected)
    except CMakeError as error:
        raise CMakeError(
            f"Profile {display_value(profile.name)} is not usable: {error} "
            "Resolve the cache/path problem, then run "
            f"cmake_configure(profiles=[{display_value(profile.name)}])."
        ) from error
    source = cache.get("CMAKE_HOME_DIRECTORY")
    if source is None or Path(source).resolve() != project:
        raise CMakeError(
            f"build-directory={display_value(str(profile.build_directory))} for profile "
            f"{display_value(profile.name)} belongs to another source project. "
            "The operator must select a build directory configured for this workspace."
        )
    return cache


def execution_paths(
    executable: WorkspacePath,
    cwd: WorkspacePath,
    project: Path,
    storage: Path,
) -> tuple[Path, Path]:
    """Resolve direct-test paths and require an existing native program and directory."""
    program = execution_path(executable, project, storage, parameter="executable")
    directory = execution_path(cwd, project, storage, parameter="cwd")
    if not program.is_file():
        raise CMakeError(
            f"executable={display_value(str(executable))} is missing or is not a "
            "file. Select an existing built test executable; use "
            "workspace_find_files(pattern='*', path='project/') to locate files."
        )
    if not directory.is_dir():
        raise CMakeError(
            f"cwd={display_value(str(cwd))} is missing or is not a directory. "
            "Choose an existing project/ or storage/ directory."
        )
    if os.name == "nt" and program.suffix.lower() in (".bat", ".cmd"):
        raise CMakeError(
            f"executable={display_value(str(executable))} is a Windows batch file. "
            "cmake_run_test requires a native executable such as the built test program."
        )
    return program, directory
