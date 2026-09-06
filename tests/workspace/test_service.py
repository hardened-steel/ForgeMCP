from pathlib import Path

import pytest

from forgemcp.workspace.errors import (
    UnsupportedExtensionError,
    WorkspaceNotDirectoryError,
    WorkspaceNotFoundError,
)
from forgemcp.workspace.service import WorkspaceService


def test_overview_counts_cpp_files(cpp_acceptance_project: Path) -> None:
    overview = WorkspaceService(cpp_acceptance_project).overview()

    assert overview.source_files == 12
    assert overview.header_files == 3
    assert overview.files_by_extension == {"cpp": 12, "hpp": 3}
    assert overview.has_cmake_lists is True
    assert overview.has_cmake_presets is True
    assert overview.scan_truncated is False


def test_files_are_relative_sorted_and_filtered(cpp_acceptance_project: Path) -> None:
    files, truncated = WorkspaceService(cpp_acceptance_project).files(".cpp")

    assert files == [
        "analysis/clangd_anchors.cpp",
        "analysis/code_action.cpp",
        "analysis/format_me.cpp",
        "analysis/tidy_me.cpp",
        "app/good_main.cpp",
        "app/warning_main.cpp",
        "debug/debug_main.cpp",
        "negative/compile_error.cpp",
        "negative/link_error.cpp",
        "src/hierarchy.cpp",
        "src/math.cpp",
        "tests/test_main.cpp",
    ]
    assert truncated is False


def test_rejects_unsupported_extension(cpp_acceptance_project: Path) -> None:
    with pytest.raises(UnsupportedExtensionError):
        WorkspaceService(cpp_acceptance_project).files("py")


def test_rejects_missing_workspace(cpp_acceptance_project: Path) -> None:
    with pytest.raises(WorkspaceNotFoundError):
        WorkspaceService(cpp_acceptance_project / "missing")


def test_rejects_workspace_file(cpp_acceptance_project: Path) -> None:
    with pytest.raises(WorkspaceNotDirectoryError):
        WorkspaceService(cpp_acceptance_project / "README.md")
