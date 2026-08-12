"""Unit tests for the transport-neutral, one-root Workspace service."""

from __future__ import annotations

from pathlib import Path

import pytest

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.models import FileChangeKind, Position, Range
import forgemcp.workspace.service as workspace_service_module
from forgemcp.workspace import (
    PatchCommitError,
    SymlinkWorkspacePathError,
    WorkspaceEncodingError,
    WorkspaceFileTooLargeError,
    WorkspacePathError,
    WorkspacePolicy,
    WorkspaceService,
    WorkspaceTextEdit,
    WorkspaceTextEditError,
)


def workspace(root: Path, *, policy: WorkspacePolicy | None = None) -> WorkspaceService:
    """Create an isolated service with production configuration dependencies."""
    return WorkspaceService(ForgeConfig(workspace_root=root), create_logger("CRITICAL"), policy=policy)


def unified_change(path: str, old: str, new: str) -> str:
    """Create a minimal single-line unified patch for readable tests."""
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


def test_list_files_returns_snapshots_and_excludes_default_generated_directories(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.cpp").write_text("int main() {}\n", encoding="utf-8")
    (tmp_path / "top.txt").write_text("top\n", encoding="utf-8")
    for ignored in (".git", ".venv", "build", "cmake-build-debug"):
        directory = tmp_path / ignored
        directory.mkdir()
        (directory / "ignored.txt").write_text("ignored\n", encoding="utf-8")

    files = workspace(tmp_path).list_files(recursive=True)

    assert [snapshot.uri.rsplit("/", 1)[-1] for snapshot in files] == ["main.cpp", "top.txt"]
    assert all(snapshot.exists and snapshot.sha256 is not None for snapshot in files)


def test_configurable_ignore_policy_can_include_custom_build_directory(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "generated.cpp").write_text("generated\n", encoding="utf-8")
    (tmp_path / "source.cpp").write_text("source\n", encoding="utf-8")
    policy = WorkspacePolicy(ignored_directory_names=frozenset({"out"}), ignored_directory_patterns=frozenset())

    files = workspace(tmp_path, policy=policy).list_files(recursive=True)

    assert [snapshot.uri.rsplit("/", 1)[-1] for snapshot in files] == ["source.cpp"]


def test_relative_path_validation_rejects_traversal_and_absolute_paths(tmp_path):
    service = workspace(tmp_path)
    outside = tmp_path.parent / "outside.txt"

    with pytest.raises(WorkspacePathError):
        service.get_snapshot("../outside.txt")
    with pytest.raises(WorkspacePathError):
        service.get_snapshot(str(outside))
    with pytest.raises(WorkspacePathError):
        service.get_snapshot("C:relative-to-a-drive.txt")


def test_symlink_entries_are_not_listed_and_cannot_be_read_or_traversed(tmp_path):
    outside = tmp_path.parent / "workspace-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    file_link = tmp_path / "file-link.txt"
    directory_link = tmp_path / "directory-link"
    try:
        file_link.symlink_to(outside / "secret.txt")
        directory_link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symbolic links are unavailable in this test environment: {error}")

    service = workspace(tmp_path)

    assert service.list_files(recursive=True) == ()
    with pytest.raises(SymlinkWorkspacePathError):
        service.get_snapshot("file-link.txt")
    with pytest.raises(SymlinkWorkspacePathError):
        service.read_text("directory-link/secret.txt")


def test_read_text_is_utf8_bounded_and_returns_matching_snapshot(tmp_path):
    (tmp_path / "ok.txt").write_bytes("naïve\n".encode("utf-8"))
    (tmp_path / "binary.bin").write_bytes(b"\xff")
    (tmp_path / "large.txt").write_text("012345678", encoding="utf-8")
    service = workspace(tmp_path, policy=WorkspacePolicy(max_read_bytes=8))

    text, snapshot = service.read_text("ok.txt")
    assert text == "naïve\n"
    assert snapshot.size_bytes == len(text.encode("utf-8"))

    with pytest.raises(WorkspaceEncodingError):
        workspace(tmp_path).read_text("binary.bin")
    with pytest.raises(WorkspaceFileTooLargeError):
        service.read_text("large.txt")


def test_generated_directory_capability_creates_and_guards_file_api_style_files(tmp_path):
    service = workspace(tmp_path)

    generated = service.open_generated_directory("build", create=True)
    generated.write_text(".cmake/api/v1/query/codemodel-v2", "")
    generated.write_text(".cmake/api/v1/reply/index-test.json", '{"reply": {}}')

    assert generated.relative_path == "build"
    assert generated.read_text(".cmake/api/v1/reply/index-test.json") == '{"reply": {}}'
    assert generated.list_files(".cmake/api/v1/reply") == ("index-test.json",)
    assert generated.get_snapshot(".cmake/api/v1/reply/index-test.json").exists is True
    assert service.validate_reported_path(str(tmp_path / "build")) == "build"


def test_generated_directory_capability_cannot_write_outside_its_build_tree(tmp_path):
    service = workspace(tmp_path)
    generated = service.open_generated_directory("build", create=True)

    with pytest.raises(WorkspacePathError):
        generated.write_text("../outside.txt", "blocked")
    with pytest.raises(WorkspacePathError):
        generated.write_text(str(tmp_path.parent / "outside.txt"), "blocked")

    assert not (tmp_path.parent / "outside.txt").exists()


def test_generated_directory_and_reported_paths_reject_symlink_escape(tmp_path):
    outside = tmp_path.parent / "generated-outside"
    outside.mkdir()
    link = tmp_path / "build-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symbolic links are unavailable in this test environment: {error}")

    service = workspace(tmp_path)

    with pytest.raises(SymlinkWorkspacePathError):
        service.open_generated_directory("build-link", create=True)
    with pytest.raises(SymlinkWorkspacePathError):
        service.validate_reported_path(str(link / "escape.txt"))


def test_snapshot_conflict_returns_failed_result_and_preserves_external_change(tmp_path):
    target = tmp_path / "note.txt"
    target.write_text("before\n", encoding="utf-8")
    service = workspace(tmp_path)
    before = service.get_snapshot("note.txt")
    target.write_text("external edit\n", encoding="utf-8")

    result = service.apply_unified_patch(
        unified_change("note.txt", "before", "after"), {"note.txt": before}
    )

    assert result.applied is False
    assert result.changes == ()
    assert target.read_text(encoding="utf-8") == "external edit\n"


def test_hunk_failure_is_atomic_across_multiple_files(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    service = workspace(tmp_path)
    expected = {
        "first.txt": service.get_snapshot("first.txt"),
        "second.txt": service.get_snapshot("second.txt"),
    }
    patch = (
        unified_change("first.txt", "one", "ONE")
        + unified_change("second.txt", "not-two", "TWO")
    )

    result = service.apply_unified_patch(patch, expected)

    assert result.applied is False
    assert result.changes == ()
    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "two\n"


def test_commit_failure_restores_previously_replaced_files(tmp_path, monkeypatch):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    service = workspace(tmp_path)
    expected = {
        "first.txt": service.get_snapshot("first.txt"),
        "second.txt": service.get_snapshot("second.txt"),
    }
    patch = unified_change("first.txt", "one", "ONE") + unified_change("second.txt", "two", "TWO")
    real_replace = workspace_service_module.os.replace
    replacement_calls = 0

    def fail_second_target_replace(source, destination):
        nonlocal replacement_calls
        replacement_calls += 1
        if replacement_calls == 4:
            raise OSError("simulated replacement failure")
        return real_replace(source, destination)

    monkeypatch.setattr(workspace_service_module.os, "replace", fail_second_target_replace)

    with pytest.raises(PatchCommitError):
        service.apply_unified_patch(patch, expected)

    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "two\n"


def test_creation_requires_an_absent_snapshot_and_reports_existing_models(tmp_path):
    service = workspace(tmp_path)
    absent = service.get_snapshot("created.txt")
    patch = "--- /dev/null\n+++ b/created.txt\n@@ -0,0 +1 @@\n+created\n"

    result = service.apply_unified_patch(patch, {"created.txt": absent})

    assert result.applied is True
    assert result.changes[0].kind is FileChangeKind.CREATED
    assert result.changes[0].before is None
    assert result.changes[0].after is not None
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "created\n"


def test_apply_text_edits_handles_multiple_code_point_edits_in_one_file(tmp_path):
    target = tmp_path / "source.cpp"
    target.write_text("A😀BC\none\n", encoding="utf-8")
    service = workspace(tmp_path)
    before = service.get_snapshot("source.cpp")

    result = service.apply_text_edits(
        {
            "source.cpp": (
                WorkspaceTextEdit(
                    range=Range(start=Position(line=0, column=1), end=Position(line=0, column=2)),
                    new_text="X",
                ),
                WorkspaceTextEdit(
                    range=Range(start=Position(line=1, column=0), end=Position(line=1, column=3)),
                    new_text="ONE",
                ),
            )
        },
        {"source.cpp": before},
    )

    assert result.applied is True
    assert len(result.changes) == 1
    assert target.read_text(encoding="utf-8") == "AXBC\nONE\n"


def test_apply_text_edits_is_atomic_for_multiple_files_and_snapshot_conflicts(tmp_path):
    first = tmp_path / "first.cpp"
    second = tmp_path / "second.cpp"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    service = workspace(tmp_path)
    expected = {"first.cpp": service.get_snapshot("first.cpp"), "second.cpp": service.get_snapshot("second.cpp")}
    second.write_text("external\n", encoding="utf-8")

    result = service.apply_text_edits(
        {
            "first.cpp": (WorkspaceTextEdit(Range(start=Position(line=0, column=0), end=Position(line=0, column=3)), "ONE"),),
            "second.cpp": (WorkspaceTextEdit(Range(start=Position(line=0, column=0), end=Position(line=0, column=3)), "TWO"),),
        },
        expected,
    )

    assert result.applied is False
    assert result.changes == ()
    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "external\n"


def test_apply_text_edits_rejects_overlaps_without_changing_the_file(tmp_path):
    target = tmp_path / "source.cpp"
    target.write_text("abcdef\n", encoding="utf-8")
    service = workspace(tmp_path)

    with pytest.raises(WorkspaceTextEditError, match="overlap"):
        service.apply_text_edits(
            {
                "source.cpp": (
                    WorkspaceTextEdit(Range(start=Position(line=0, column=1), end=Position(line=0, column=4)), "X"),
                    WorkspaceTextEdit(Range(start=Position(line=0, column=3), end=Position(line=0, column=5)), "Y"),
                )
            },
            {"source.cpp": service.get_snapshot("source.cpp")},
        )

    assert target.read_text(encoding="utf-8") == "abcdef\n"


def test_apply_text_edits_handles_crlf_empty_eof_and_adjacent_insertions(tmp_path):
    crlf_target = tmp_path / "crlf.cpp"
    empty_target = tmp_path / "empty.cpp"
    crlf_target.write_bytes(b"abcdef\r\n")
    empty_target.write_text("", encoding="utf-8")
    service = workspace(tmp_path)

    result = service.apply_text_edits(
        {
            "crlf.cpp": (
                WorkspaceTextEdit(
                    Range(start=Position(line=0, column=1), end=Position(line=0, column=4)), "X"
                ),
                WorkspaceTextEdit(
                    Range(start=Position(line=0, column=1), end=Position(line=0, column=1)), "["
                ),
                WorkspaceTextEdit(
                    Range(start=Position(line=0, column=4), end=Position(line=0, column=4)), "]"
                ),
            ),
            "empty.cpp": (
                WorkspaceTextEdit(
                    Range(start=Position(line=0, column=0), end=Position(line=0, column=0)), "created"
                ),
            ),
        },
        {
            "crlf.cpp": service.get_snapshot("crlf.cpp"),
            "empty.cpp": service.get_snapshot("empty.cpp"),
        },
    )

    assert result.applied is True
    assert crlf_target.read_bytes() == b"a[X]ef\r\n"
    assert empty_target.read_text(encoding="utf-8") == "created"


def test_apply_text_edits_rejects_ambiguous_same_boundary_insertions(tmp_path):
    target = tmp_path / "source.cpp"
    target.write_text("abc\n", encoding="utf-8")
    service = workspace(tmp_path)

    with pytest.raises(WorkspaceTextEditError, match="overlap"):
        service.apply_text_edits(
            {
                "source.cpp": (
                    WorkspaceTextEdit(
                        Range(start=Position(line=0, column=1), end=Position(line=0, column=1)), "X"
                    ),
                    WorkspaceTextEdit(
                        Range(start=Position(line=0, column=1), end=Position(line=0, column=1)), "Y"
                    ),
                )
            },
            {"source.cpp": service.get_snapshot("source.cpp")},
        )

    assert target.read_text(encoding="utf-8") == "abc\n"
