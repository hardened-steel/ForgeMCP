import os
from pathlib import Path

import pytest

from forgemcp.workspace.errors import WorkspaceError
from forgemcp.workspace.service import WorkspaceService


@pytest.fixture
def workspace(cpp_acceptance_project):
    return WorkspaceService(
        cpp_acceptance_project,
        cpp_acceptance_project / "service-storage",
    )


def test_roots_are_validated_and_storage_is_lazy(workspace):
    assert (
        WorkspaceService(workspace.root).storage_root
        == workspace.root.parent / f".{workspace.root.name}.forgemcp"
    )
    assert not workspace.storage_root.exists()
    assert (
        WorkspaceService(workspace.root, workspace.root / "custom").storage_root
        == workspace.root / "custom"
    )
    for root in (workspace.root / "absent", workspace.root / "README.md"):
        with pytest.raises(WorkspaceError):
            WorkspaceService(root)
    for storage in (
        workspace.root,
        workspace.root.parent,
        workspace.root / "README.md",
    ):
        with pytest.raises(WorkspaceError):
            WorkspaceService(workspace.root, storage)


@pytest.mark.parametrize(
    "path",
    [
        "",
        "../outside",
        "a/../../b",
        "/absolute",
        "C:\\Windows",
        "C:relative",
        "\\\\server\\share",
        "a\0b",
    ],
)
def test_path_escape_rejected(workspace, path):
    with pytest.raises(WorkspaceError):
        workspace.resolve_path(path)


def test_tree_depth_hidden_directories_and_file_error(workspace):
    workspace.mkdir(".hidden/nested")
    workspace.write_file(".hidden/nested/value.txt", "data")
    shallow = workspace.list_directory()
    assert all(entry.children is None for entry in shallow.entries)
    hidden = next(
        entry
        for entry in workspace.list_directory(depth=None).entries
        if entry.path == ".hidden"
    )
    assert hidden.children[0].children[0].path == ".hidden/nested/value.txt"
    for kwargs in ({"path": "README.md"}, {"depth": 0}, {"path": "missing"}):
        with pytest.raises(WorkspaceError):
            workspace.list_directory(**kwargs)


def test_find_globs_extensions_and_hidden_directory_policy(workspace):
    workspace.mkdir(".hidden")
    workspace.write_file(".hidden/ignored.cpp", "needle")
    workspace.write_file(".clangd", "needle")
    workspace.write_file("UPPER.CPP", "needle")
    workspace.write_file("LICENSE", "needle")
    workspace.mkdir("build")
    workspace.write_file("build/generated.cpp", "needle")
    files = workspace.find_files(extensions=[".CPP"]).paths
    assert "UPPER.CPP" in files and "build/generated.cpp" in files
    assert ".hidden/ignored.cpp" not in files
    assert workspace.find_files(path=".hidden").paths == []
    assert workspace.read_file(".hidden/ignored.cpp").text == "needle"
    assert ".clangd" in workspace.find_files().paths
    assert workspace.find_files(extensions=[]).paths == []
    assert "LICENSE" in workspace.find_files(extensions=[""]).paths
    assert workspace.find_files("src/*.cpp").paths == [
        "src/hierarchy.cpp",
        "src/math.cpp",
    ]
    assert "UPPER.CPP" in workspace.find_files("**/*.CPP").paths
    assert workspace.find_files("*.cpp", path="src").paths == [
        "src/hierarchy.cpp",
        "src/math.cpp",
    ]


def test_read_write_metadata_and_utf8_crlf(workspace):
    text = "α\r\n日本語\r\nlast"
    created = workspace.write_file("utf8.txt", text)
    assert (created.action, created.lines_removed, created.lines_added) == (
        "created",
        0,
        3,
    )
    assert workspace.read_bytes("utf8.txt") == text.encode()
    assert (
        workspace.read_file("utf8.txt", start_line=2, end_line=2).text == "日本語\r\n"
    )
    assert workspace.read_file("utf8.txt", start_line=99).text == ""
    info = workspace.file_info("utf8.txt")
    assert info.size_bytes == len(text.encode())
    assert info.modified_at.utcoffset().total_seconds() == 0
    if os.name == "nt":
        assert info.created_at is not None and info.owner
    overwritten = workspace.write_file("utf8.txt", "new\n")
    assert (overwritten.action, overwritten.lines_removed, overwritten.lines_added) == (
        "overwritten",
        3,
        1,
    )
    assert set(workspace.read_file("utf8.txt").model_dump()) == {
        "root",
        "path",
        "text",
        "start_line",
    }
    for bounds in ({"start_line": 0}, {"start_line": 3, "end_line": 2}):
        with pytest.raises(WorkspaceError):
            workspace.read_file("utf8.txt", **bounds)


def test_exact_edit_is_unambiguous_and_preserves_unaffected_bytes(workspace):
    workspace.write_file("edit.txt", "α\r\nrepeat repeat\r\nend")
    for old in ("", "missing", "repeat"):
        with pytest.raises(WorkspaceError):
            workspace.edit_file("edit.txt", old, "changed")
        assert workspace.read_bytes("edit.txt") == "α\r\nrepeat repeat\r\nend".encode()
    assert (
        workspace.edit_file("edit.txt", "repeat", "x", replace_all=True).replacements
        == 2
    )
    assert workspace.edit_file("edit.txt", "α\r\nx x", "β").replacements == 1
    assert workspace.read_bytes("edit.txt") == "β\r\nend".encode()
    workspace.edit_file("edit.txt", "β", "")
    assert workspace.read_file("edit.txt").text == "\r\nend"


def test_failed_replace_keeps_file_and_cleans_temporary_file(workspace, monkeypatch):
    workspace.write_file("keep.txt", "original")

    def fail(source, destination):
        assert Path(source).parent == Path(destination).parent
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(WorkspaceError):
        workspace.write_file("keep.txt", "replacement")
    assert workspace.read_file("keep.txt").text == "original"
    assert not list(workspace.root.glob(".forgemcp-*"))


def test_search_literals_regex_extensions_and_skips(workspace):
    workspace.mkdir("search")
    workspace.write_file("search/a.cpp", "a.b a.b\nAxb\nfoo(x)/../#&\n")
    workspace.write_file("search/b.txt", "a.b")
    (workspace.root / "search/binary").write_bytes(b"a.b\0")
    (workspace.root / "search/invalid").write_bytes(b"\xff")
    result = workspace.search("a.b", path="search")
    assert [(item.path, item.line) for item in result.matches] == [
        ("search/a.cpp", 1),
        ("search/b.txt", 1),
    ]
    assert result.skipped_files == ["search/binary", "search/invalid"]
    result = workspace.search(
        "a.b",
        regex=True,
        case_sensitive=False,
        extensions=["cpp"],
        path="search",
    )
    assert [item.line for item in result.matches] == [1, 2]
    assert (
        workspace.search("foo\\(x\\)", regex=True, path="search/a.cpp").matches[0].line
        == 3
    )
    for query, use_regex in (("", False), ("[", True)):
        with pytest.raises(WorkspaceError):
            workspace.search(query, regex=use_regex)
    for path in ("search/binary", "search/invalid"):
        with pytest.raises(WorkspaceError):
            workspace.read_file(path)


def test_move_files_directories_and_empty_directory_deletion(workspace):
    assert workspace.mkdir("new/nested").action == "created"
    assert workspace.mkdir("new/nested").action == "already_exists"
    workspace.write_file("new/nested/file.txt", "content")
    workspace.move("new", "moved")
    workspace.move("moved/nested/file.txt", "moved/file.txt")
    assert workspace.read_file("moved/file.txt").text == "content"
    for source, target in (("moved/file.txt", "README.md"), ("moved", "moved/child")):
        with pytest.raises(WorkspaceError):
            workspace.move(source, target)
    with pytest.raises(WorkspaceError):
        workspace.delete("moved")
    workspace.delete("moved/file.txt")
    workspace.delete("moved/nested")
    workspace.delete("moved")
    with pytest.raises(WorkspaceError):
        workspace.delete("moved")


def test_protected_paths_and_parents_apply_to_all_mutations(workspace):
    workspace.mkdir("repo/.git")
    workspace.write_file("repo/.git/config", "data")
    workspace.protect_path("repo/.git")
    workspace.protect_path("repo/.git")
    assert workspace.read_file("repo/.git/config").text == "data"
    operations = [
        lambda: workspace.write_file("repo/.git/config", "x"),
        lambda: workspace.edit_file("repo/.git/config", "data", "x"),
        lambda: workspace.delete("repo/.git/config"),
        lambda: workspace.mkdir("repo/.git/new"),
        lambda: workspace.move("repo", "elsewhere"),
        lambda: workspace.move("README.md", "repo/.git/new"),
    ]
    for operation in operations:
        with pytest.raises(WorkspaceError, match="protected"):
            operation()
    workspace.protect_path("reserved")
    with pytest.raises(WorkspaceError, match="protected"):
        workspace.write_file("reserved", "x")
    for root in ("project", "storage"):
        for operation in (workspace.delete, workspace.mkdir):
            with pytest.raises(WorkspaceError, match="root"):
                operation(".", root=root)


def test_storage_persistence_nesting_cleanup_and_protection(workspace):
    directory = workspace.storage_directory("build").subdirectory("debug")
    workspace.write_file("build/debug/state", "cache", root="storage")
    restarted = WorkspaceService(workspace.root, workspace.storage_root)
    assert (
        restarted.storage_directory("build").subdirectory("debug").path
        == directory.path
    )
    assert restarted.read_file("build/debug/state", root="storage").text == "cache"
    with pytest.raises(RuntimeError):
        with workspace.temporary_directory("test") as temporary:
            temporary.subdirectory("nested")
            assert temporary.path.is_dir()
            raise RuntimeError("operation failed")
    assert not temporary.path.exists()
    assert directory.path.exists()
    workspace.protect_path("build/debug/state", root="storage")
    with pytest.raises(WorkspaceError, match="protected"):
        directory.remove()
    removable = workspace.storage_directory("removable")
    removable.subdirectory("child")
    removable.remove()
    removable.remove()
    for key in ("..", "a/b", "", "a\\b"):
        with pytest.raises(WorkspaceError):
            workspace.storage_directory(key)


def test_links_are_visible_but_not_traversed_or_modified(workspace):
    target = workspace.root / "src"
    alias = workspace.root / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this host")
    outside = workspace.root / "outside"
    outside.symlink_to(workspace.root.parent, target_is_directory=True)
    entries = workspace.list_directory(depth=None).entries
    assert next(entry for entry in entries if entry.path == "alias").kind == "symlink"
    assert not any(path.startswith("alias/") for path in workspace.find_files().paths)
    assert (
        workspace.read_file("alias/math.cpp").text
        == workspace.read_file("src/math.cpp").text
    )
    with pytest.raises(WorkspaceError):
        workspace.write_file("alias/math.cpp", "x")
    with pytest.raises(WorkspaceError):
        workspace.read_file("outside/secret")


def test_markdown_escapes_source_fences_and_link_characters(workspace):
    workspace.write_file("fence.txt", "```\n<script>unsafe</script>\n")
    result = workspace.render_markdown(workspace.search("```", path="fence.txt"))
    assert "````\n```\n````" in result
    assert workspace.file_uri("project", "a#b%[x].cpp").endswith("a%23b%25%5Bx%5D.cpp")
