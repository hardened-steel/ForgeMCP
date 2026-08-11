import pytest

from forgemcp.config import ForgeConfig
from forgemcp.workspace import WorkspaceError, WorkspaceService


def test_resolve_path_rejects_escape(tmp_path):
    workspace = WorkspaceService(ForgeConfig(workspace_root=tmp_path))

    with pytest.raises(WorkspaceError, match="inside the configured workspace"):
        workspace.resolve_path("../outside")


def test_read_text_marks_truncated_content(tmp_path):
    (tmp_path / "source.cpp").write_text("abcdef", encoding="utf-8")
    workspace = WorkspaceService(ForgeConfig(workspace_root=tmp_path))

    result = workspace.read_text("source.cpp", max_chars=3)

    assert result.content == "abc"
    assert result.truncated is True


def test_list_files_excludes_hidden_paths(tmp_path):
    (tmp_path / "main.cpp").touch()
    hidden = tmp_path / ".cache"
    hidden.mkdir()
    (hidden / "index").touch()
    workspace = WorkspaceService(ForgeConfig(workspace_root=tmp_path))

    assert workspace.list_files(recursive=True) == ["main.cpp"]
