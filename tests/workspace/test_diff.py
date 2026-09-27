import pytest

from forgemcp.workspace.diff import create_diff, diff_extension
from forgemcp.workspace.extensions import ExtensionContext, TextChange
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import FileWriteResult


@pytest.fixture
def anyio_backend():
    return "asyncio"


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
    change = TextChange(path=WorkspacePath("project/example.cpp"), before=before, after=after)
    result = create_diff(change)
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
    before = [f"line {number}\n" for number in range(40)]
    after = before.copy()
    after[1] = "first change\n"
    after[38] = "last change\n"
    diff = create_diff(
        TextChange(
            path=WorkspacePath("project/example.cpp"),
            before="".join(before),
            after="".join(after),
        )
    )
    assert len(diff.hunks) == 2
    assert [change.before_start for change in diff.changes] == [2, 39]
    assert all(change.before_count == change.after_count == 1 for change in diff.changes)
    assert all(change.kind == "replace" for change in diff.changes)


def test_unchanged_text_has_empty_diff():
    diff = create_diff(TextChange(path=WorkspacePath("project/a"), before="same", after="same"))
    assert diff.hunks == diff.changes == []


def test_character_spans_use_unicode_code_points_and_preserve_unchanged_parts():
    diff = create_diff(
        TextChange(
            path=WorkspacePath("project/a"),
            before="😀 value = old;\n",
            after="😀 value = newer;\n",
        )
    )
    removed, added = diff.hunks[0].lines
    assert "".join(removed.text[a:b] for a, b in removed.spans) == "old"
    assert "".join(added.text[a:b] for a, b in added.spans) == "newer"


@pytest.mark.anyio
async def test_provider_skips_results_without_a_mutation():
    result = FileWriteResult(path=WorkspacePath("project/a"), action="created", lines_added=0, lines_removed=0)
    assert await diff_extension(ExtensionContext(tool_name="workspace_read_file", result=result, paths=())) is None
