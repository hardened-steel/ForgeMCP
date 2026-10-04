"""Typed diff reconstruction, context grouping, and Unicode change-span tests."""

import pytest

from forgemcp.workspace.diff import create_diff
from forgemcp.workspace.path import WorkspacePath


@pytest.mark.parametrize(
    "before,after",
    [
        ("", "new\n"),
        ("old\n", ""),
        ("old", "new"),
        ("same\nold\nend", "same\nnew\nend"),
        ("α\r\n日本語\r\n", "α\r\n変更\r\n"),
        ("line", "line\n"),
        ("line\r\n", "line\n"),
    ],
)
def test_typed_diff_preserves_exact_text_and_line_numbers(before, after):
    """Verify diff lines reconstruct both texts exactly with their original line numbers."""
    result = create_diff(WorkspacePath("project/example.cpp"), before, after)
    lines = [line for hunk in result.hunks for line in hunk.lines]
    assert "".join(line.text for line in lines if line.kind != "added") == before
    assert "".join(line.text for line in lines if line.kind != "removed") == after
    assert [line.before_line for line in lines if line.kind != "added"] == list(
        range(1, len(before.splitlines()) + 1)
    )
    assert [line.after_line for line in lines if line.kind != "removed"] == list(
        range(1, len(after.splitlines()) + 1)
    )
    assert all(line.before_line is None for line in lines if line.kind == "added")
    assert all(line.after_line is None for line in lines if line.kind == "removed")


def test_distant_changes_have_separate_hunks_and_compact_ranges():
    """Verify distant edits produce separate context hunks and compact replacement ranges."""
    before = [f"line {number}\n" for number in range(40)]
    after = before.copy()
    after[1] = "first change\n"
    after[38] = "last change\n"
    diff = create_diff(
        WorkspacePath("project/example.cpp"),
        "".join(before),
        "".join(after),
    )
    assert len(diff.hunks) == 2
    assert [change.before_start for change in diff.changes] == [2, 39]
    assert all(change.before_count == change.after_count == 1 for change in diff.changes)
    assert all(change.kind == "replace" for change in diff.changes)


def test_unchanged_text_has_empty_diff():
    """Verify identical input texts produce no changes or hunks."""
    diff = create_diff(WorkspacePath("project/a"), "same", "same")
    assert diff.hunks == diff.changes == []


def test_character_spans_use_unicode_code_points_and_preserve_unchanged_parts():
    """Verify changed spans use Unicode offsets and exclude unchanged surrounding characters."""
    diff = create_diff(
        WorkspacePath("project/a"),
        "😀 value = old;\n",
        "😀 value = newer;\n",
    )
    removed, added = diff.hunks[0].lines
    assert "".join(removed.text[a:b] for a, b in removed.spans) == "old"
    assert "".join(added.text[a:b] for a, b in added.spans) == "newer"
