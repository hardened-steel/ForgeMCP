import asyncio
import os
from unittest.mock import AsyncMock
from types import SimpleNamespace
from pathlib import Path

import pytest

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver.exceptions import ToolError
from forgemcp.completion import Complete

from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.errors import WorkspaceError
from forgemcp.workspace.service import ReadConfirmation, WorkspaceService


@pytest.fixture
def workspace(cpp_acceptance_project):
    return WorkspaceService(
        cpp_acceptance_project,
        cpp_acceptance_project / "service-storage",
    )


@pytest.fixture
def call(workspace):
    apps = Apps()
    workspace.register(MCPServer("test", extensions=[apps]), apps, Complete())
    handlers = {binding.fn.__name__: binding.fn for binding in apps.tools()}

    def invoke(name, *args, **kwargs):
        if "path" in kwargs and isinstance(kwargs["path"], str):
            kwargs["path"] = WorkspacePath("project/" + kwargs["path"])
        if name == "workspace_read_file":
            kwargs["confirm"] = ReadConfirmation(allow=True)
        return asyncio.run(
            handlers[name](
                *args,
                ctx=SimpleNamespace(report_progress=AsyncMock()),
                **kwargs,
            )
        )

    return invoke


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


def test_tree_depth_hidden_directories_and_file_error(workspace, call):
    call("workspace_mkdir", WorkspacePath("project/.hidden/nested"))
    call("workspace_write_file", WorkspacePath("project/.hidden/nested/value.txt"), "data")
    call("workspace_write_file", WorkspacePath("project/.dotfile"), "visible")
    shallow = call(
        "workspace_list",
    )
    assert ".hidden" not in {entry.path.relative for entry in shallow.entries}
    assert ".dotfile" in {entry.path.relative for entry in shallow.entries}
    assert all(entry.children is None for entry in shallow.entries)
    hidden = next(
        entry
        for entry in call("workspace_list", depth=None, include_hidden=True).entries
        if entry.path.relative == ".hidden"
    )
    assert hidden.children[0].children[0].path.relative == ".hidden/nested/value.txt"
    for kwargs in ({"path": "README.md"}, {"depth": 0}, {"path": "missing"}):
        with pytest.raises(ToolError):
            call("workspace_list", **kwargs)


def test_find_globs_extensions_and_hidden_directory_policy(workspace, call):
    call("workspace_mkdir", WorkspacePath("project/.hidden"))
    call("workspace_write_file", WorkspacePath("project/.hidden/ignored.cpp"), "needle")
    call("workspace_write_file", WorkspacePath("project/.clangd"), "needle")
    call("workspace_write_file", WorkspacePath("project/UPPER.CPP"), "needle")
    call("workspace_write_file", WorkspacePath("project/LICENSE"), "needle")
    call("workspace_mkdir", WorkspacePath("project/build"))
    call("workspace_write_file", WorkspacePath("project/build/generated.cpp"), "needle")
    files = [path.relative for path in call("workspace_find_files").paths]
    assert "UPPER.CPP" in files and "build/generated.cpp" in files
    assert ".hidden/ignored.cpp" not in files
    assert [path.relative for path in call("workspace_find_files", path=".hidden").paths] == []
    assert call("workspace_read_file", WorkspacePath("project/.hidden/ignored.cpp")).text == "needle"
    assert (
        ".clangd"
        in [path.relative for path in call(
            "workspace_find_files",
        ).paths]
    )
    assert "LICENSE" in [path.relative for path in call("workspace_find_files", pattern="LICENSE").paths]
    assert [path.relative for path in call("workspace_find_files", pattern="src/*.cpp").paths] == [
        "src/hierarchy.cpp",
        "src/math.cpp",
    ]
    assert "UPPER.CPP" in [path.relative for path in call("workspace_find_files", pattern="**/*.CPP").paths]
    assert [path.relative for path in call("workspace_find_files", pattern="*.cpp", path="src").paths] == [
        "src/hierarchy.cpp",
        "src/math.cpp",
    ]
    found = call("workspace_find_files", pattern="*.cpp", path="src")
    assert [file.path for file in found.files] == found.paths
    assert all(file.size_bytes >= 0 and file.modified_at for file in found.files)


def test_read_write_metadata_and_utf8_crlf(workspace, call):
    text = "α\r\n日本語\r\nlast"
    created = call(
        "workspace_write_file",
        WorkspacePath("project/utf8.txt"),
        text,
    )
    assert "diff" not in created.model_dump()
    assert (created.action, created.lines_removed, created.lines_added) == (
        "created",
        0,
        3,
    )
    assert (workspace.root / "utf8.txt").read_bytes() == text.encode()
    assert (
        call("workspace_read_file", WorkspacePath("project/utf8.txt"), start_line=2, end_line=2).text == "日本語\r\n"
    )
    assert call("workspace_read_file", WorkspacePath("project/utf8.txt"), start_line=99).text == ""
    info = call("workspace_file_info", WorkspacePath("project/utf8.txt"))
    assert info.size_bytes == len(text.encode())
    assert info.modified_at.utcoffset().total_seconds() == 0
    if os.name == "nt":
        assert info.created_at is not None and info.owner
    overwritten = call(
        "workspace_write_file",
        WorkspacePath("project/utf8.txt"),
        "new\n",
    )
    assert "diff" not in overwritten.model_dump()
    assert (overwritten.action, overwritten.lines_removed, overwritten.lines_added) == (
        "overwritten",
        3,
        1,
    )
    assert set(call("workspace_read_file", WorkspacePath("project/utf8.txt")).model_dump()) == {
        "resources",
        "path",
        "text",
        "start_line",
    }
    for bounds in ({"start_line": 0}, {"start_line": 3, "end_line": 2}):
        with pytest.raises(ToolError):
            call("workspace_read_file", WorkspacePath("project/utf8.txt"), **bounds)


def test_exact_edit_is_unambiguous_and_preserves_unaffected_bytes(workspace, call):
    call("workspace_write_file", WorkspacePath("project/edit.txt"), "α\r\nrepeat repeat\r\nend")
    for old in ("", "missing", "repeat"):
        with pytest.raises(ToolError):
            call("workspace_edit_file", WorkspacePath("project/edit.txt"), old, "changed")
        assert (workspace.root / "edit.txt").read_bytes() == "α\r\nrepeat repeat\r\nend".encode()
    first_edit = call(
        "workspace_edit_file",
        WorkspacePath("project/edit.txt"),
        "repeat",
        "x",
        replace_all=True,
    )
    assert first_edit.replacements == 2
    assert "diff" not in first_edit.model_dump()
    assert "changed_lines" not in first_edit.model_dump()
    assert call("workspace_edit_file", WorkspacePath("project/edit.txt"), "α\r\nx x", "β").replacements == 1
    assert (workspace.root / "edit.txt").read_bytes() == "β\r\nend".encode()
    call("workspace_edit_file", WorkspacePath("project/edit.txt"), "β", "")
    assert call("workspace_read_file", WorkspacePath("project/edit.txt")).text == "\r\nend"


def test_failed_replace_keeps_file_and_cleans_temporary_file(workspace, call, monkeypatch):
    call("workspace_write_file", WorkspacePath("project/keep.txt"), "original")

    def fail(source, destination):
        assert Path(source).parent == Path(destination).parent
        raise PermissionError("locked")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(ToolError):
        call(
            "workspace_write_file",
            WorkspacePath("project/keep.txt"),
            "replacement",
        )
    assert call("workspace_read_file", WorkspacePath("project/keep.txt")).text == "original"
    assert not list(workspace.root.glob(".forgemcp-*"))


def test_search_literals_regex_extensions_and_skips(workspace, call):
    call("workspace_mkdir", WorkspacePath("project/search"))
    call("workspace_write_file", WorkspacePath("project/search/a.cpp"), "a.b a.b\nAxb\nfoo(x)/../#&\n")
    call("workspace_write_file", WorkspacePath("project/search/b.txt"), "a.b")
    (workspace.root / "search/binary").write_bytes(b"a.b\0")
    (workspace.root / "search/invalid").write_bytes(b"\xff")
    result = call("workspace_search", "a.b", path="search")
    assert [(item.path.relative, item.line) for item in result.matches] == [
        ("search/a.cpp", 1),
        ("search/b.txt", 1),
    ]
    assert [path.relative for path in result.skipped_files] == ["search/binary", "search/invalid"]
    result = call(
        "workspace_search",
        "a.b",
        regex=True,
        case_sensitive=False,
        extensions=["cpp"],
        path="search",
    )
    assert [item.line for item in result.matches] == [1, 2]
    assert (
        call("workspace_search", "foo\\(x\\)", regex=True, path="search/a.cpp")
        .matches[0]
        .line
        == 3
    )
    for query, use_regex in (("", False), ("[", True)):
        with pytest.raises(ToolError):
            call("workspace_search", query=query, regex=use_regex)
    for path in ("search/binary", "search/invalid"):
        with pytest.raises(ToolError):
            call("workspace_read_file", WorkspacePath("project/" + path))


def test_move_files_directories_and_empty_directory_deletion(workspace, call):
    assert call("workspace_mkdir", WorkspacePath("project/new/nested")).action == "created"
    assert call("workspace_mkdir", WorkspacePath("project/new/nested")).action == "already_exists"
    call("workspace_write_file", WorkspacePath("project/new/nested/file.txt"), "content")
    call("workspace_move", WorkspacePath("project/new"), WorkspacePath("project/moved"))
    call("workspace_move", WorkspacePath("project/moved/nested/file.txt"), WorkspacePath("project/moved/file.txt"))
    assert call("workspace_read_file", WorkspacePath("project/moved/file.txt")).text == "content"
    for source, target in (("moved/file.txt", "README.md"), ("moved", "moved/child")):
        with pytest.raises(ToolError):
            call("workspace_move", WorkspacePath("project/" + source), WorkspacePath("project/" + target))
    with pytest.raises(ToolError):
        call("workspace_delete", WorkspacePath("project/moved"))
    call("workspace_delete", WorkspacePath("project/moved/file.txt"))
    call("workspace_delete", WorkspacePath("project/moved/nested"))
    call("workspace_delete", WorkspacePath("project/moved"))
    with pytest.raises(ToolError):
        call("workspace_delete", WorkspacePath("project/moved"))


def test_protected_paths_and_parents_apply_to_all_mutations(workspace, call):
    call("workspace_mkdir", WorkspacePath("project/repo/.git"))
    call("workspace_write_file", WorkspacePath("project/repo/.git/config"), "data")
    workspace.protect_path(WorkspacePath("project/repo/.git"))
    workspace.protect_path(WorkspacePath("project/repo/.git"))
    assert call("workspace_read_file", WorkspacePath("project/repo/.git/config")).text == "data"
    operations = [
        lambda: call("workspace_write_file", WorkspacePath("project/repo/.git/config"), "x"),
        lambda: call("workspace_edit_file", WorkspacePath("project/repo/.git/config"), "data", "x"),
        lambda: call("workspace_delete", WorkspacePath("project/repo/.git/config")),
        lambda: call("workspace_mkdir", WorkspacePath("project/repo/.git/new")),
        lambda: call("workspace_move", WorkspacePath("project/repo"), WorkspacePath("project/elsewhere")),
        lambda: call("workspace_move", WorkspacePath("project/README.md"), WorkspacePath("project/repo/.git/new")),
    ]
    for operation in operations:
        with pytest.raises(ToolError, match="protected"):
            operation()
    workspace.protect_path(WorkspacePath("project/reserved"))
    with pytest.raises(ToolError, match="protected"):
        call("workspace_write_file", WorkspacePath("project/reserved"), "x")
    for root in ("project", "storage"):
        for operation in ("workspace_delete", "workspace_mkdir"):
            with pytest.raises(ToolError, match="root"):
                call(operation, WorkspacePath(root + "/"))


def test_storage_is_lazy_and_persists_across_service_restarts(workspace, call):
    assert not workspace.storage_root.exists()
    call("workspace_mkdir", WorkspacePath("storage/build/debug"))
    call("workspace_write_file", WorkspacePath("storage/build/debug/state"), "cache")
    restarted = WorkspaceService(workspace.root, workspace.storage_root)
    assert restarted.resolve_workspace_path(
        WorkspacePath("storage/build/debug/state"),
    ).read_text() == "cache"
    workspace.protect_path(WorkspacePath("storage/build/debug/state"))
    with pytest.raises(ToolError, match="protected"):
        call("workspace_delete", WorkspacePath("storage/build/debug/state"))


def test_links_are_visible_but_not_traversed_or_modified(workspace, call):
    target = workspace.root / "src"
    alias = workspace.root / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this host")
    outside = workspace.root / "outside"
    outside.symlink_to(workspace.root.parent, target_is_directory=True)
    entries = call("workspace_list", depth=None).entries
    assert next(entry for entry in entries if entry.path.relative == "alias").kind == "symlink"
    assert not any(
        path.startswith("alias/")
        for path in [path.relative for path in call(
            "workspace_find_files",
        ).paths]
    )
    assert (
        call("workspace_read_file", WorkspacePath("project/alias/math.cpp")).text
        == call("workspace_read_file", WorkspacePath("project/src/math.cpp")).text
    )
    with pytest.raises(ToolError):
        call("workspace_write_file", WorkspacePath("project/alias/math.cpp"), "x")
    with pytest.raises(ToolError):
        call("workspace_read_file", WorkspacePath("project/outside/secret"))


def test_markdown_escapes_source_fences_and_link_characters(workspace, call):
    call("workspace_write_file", WorkspacePath("project/fence.txt"), "```\n<script>unsafe</script>\n")
    result = workspace.render_markdown(
        call("workspace_search", "```", path="fence.txt")
    )
    assert "````\n```\n````" in result
    assert workspace.file_uri(WorkspacePath("project/a#b%[x].cpp")).endswith("a%23b%25%5Bx%5D.cpp")


def test_filesystem_errors_preserve_os_message_without_absolute_path():
    import errno
    from forgemcp.workspace.service import filesystem_errors

    with pytest.raises(WorkspaceError) as caught:
        with filesystem_errors("relative/path"):
            raise OSError(errno.ENOTEMPTY, "Каталог не пуст", "C:/private/path")
    assert str(caught.value) == "relative/path: Каталог не пуст."

    with pytest.raises(WorkspaceError, match="PermissionError"):
        with filesystem_errors("relative/path"):
            raise PermissionError()


@pytest.mark.parametrize("interval", [0, 0.5, 1, 2])
def test_progress_throttles_per_invocation_without_losing_results(
    workspace,
    call,
    monkeypatch,
    interval,
):
    import forgemcp.progress as module

    workspace.progress_interval = interval
    call("workspace_write_file", WorkspacePath("project/large.txt"), "needle\n" * 30000)
    apps = Apps()
    workspace.register(MCPServer("test", extensions=[apps]), apps, Complete())
    handlers = {binding.fn.__name__: binding.fn for binding in apps.tools()}
    now = -0.25

    def clock():
        nonlocal now
        now += 0.25
        return now

    monkeypatch.setattr(module, "monotonic", clock)
    for name, arguments in (
        ("workspace_list", {"depth": None}),
        ("workspace_find_files", {}),
        ("workspace_search", {"query": "needle", "path": "large.txt"}),
        ("workspace_read_file", {"path": "large.txt"}),
        ("workspace_mkdir", {"path": "new"}),
        ("workspace_mkdir", {"path": "new"}),
    ):
        events = []

        async def collect(value, total=None, message=None):
            events.append((now, value, message))

        if name == "workspace_read_file":
            arguments["confirm"] = ReadConfirmation(allow=True)
        result = asyncio.run(
            handlers[name](
                ctx=SimpleNamespace(report_progress=collect),
                **{
                    key: WorkspacePath("project/" + value) if key == "path" else value
                    for key, value in arguments.items()
                },
            )
        )
        assert events[0][1] == 0
        assert all(b[0] - a[0] >= interval for a, b in zip(events, events[1:]))
        assert all(b[1] > a[1] for a, b in zip(events, events[1:]))
        if name == "workspace_list":
            assert len(events) > 1
            assert all(event[2].startswith("process ") for event in events[1:])
        if name == "workspace_read_file":
            assert result.text == "needle\n" * 30000
        if name == "workspace_search":
            assert len(result.matches) == 30000
            assert all(event[2].startswith("Searching project/large.txt") for event in events[1:])
        if name == "workspace_mkdir":
            assert len(events) == (2 if interval == 0 else 1)
