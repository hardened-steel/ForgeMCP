"""Shared pytest fixtures for ForgeMCP tests."""

from pathlib import Path
from shutil import copytree

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CPP_ACCEPTANCE_PROJECT = REPOSITORY_ROOT / "examples" / "cpp-acceptance-project"


@pytest.fixture
def cpp_acceptance_project(tmp_path: Path) -> Path:
    """Copy the complete C++ acceptance project into an isolated workspace."""
    workspace = tmp_path / CPP_ACCEPTANCE_PROJECT.name
    copytree(CPP_ACCEPTANCE_PROJECT, workspace)
    return workspace
