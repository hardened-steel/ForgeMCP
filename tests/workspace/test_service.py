from pathlib import Path

import pytest

from forgemcp.workspace.errors import (
    UnsupportedExtensionError,
    WorkspaceNotDirectoryError,
    WorkspaceNotFoundError,
)
from forgemcp.workspace.service import WorkspaceService


def test_overview_counts_cpp_files(tmp_path: Path) -> None:
    (tmp_path / "CMakeLists.txt").write_text("project(example)", encoding="utf-8")
    (tmp_path / "main.cpp").write_text("int main() {}", encoding="utf-8")
    include = tmp_path / "include"
    include.mkdir()
    (include / "example.hpp").write_text("#pragma once", encoding="utf-8")

    overview = WorkspaceService(tmp_path).overview()

    assert overview.source_files == 1
    assert overview.header_files == 1
    assert overview.files_by_extension == {"cpp": 1, "hpp": 1}
    assert overview.has_cmake_lists is True
    assert overview.scan_truncated is False


def test_files_are_relative_sorted_and_filtered(tmp_path: Path) -> None:
    (tmp_path / "z.cpp").write_text("", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.cpp").write_text("", encoding="utf-8")
    (source / "ignored.py").write_text("", encoding="utf-8")

    files, truncated = WorkspaceService(tmp_path).files(".cpp")

    assert files == ["src/a.cpp", "z.cpp"]
    assert truncated is False


def test_rejects_unsupported_extension(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedExtensionError):
        WorkspaceService(tmp_path).files("py")


def test_rejects_missing_workspace(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceNotFoundError):
        WorkspaceService(tmp_path / "missing")


def test_rejects_workspace_file(tmp_path: Path) -> None:
    workspace_file = tmp_path / "workspace.txt"
    workspace_file.write_text("", encoding="utf-8")

    with pytest.raises(WorkspaceNotDirectoryError):
        WorkspaceService(workspace_file)
