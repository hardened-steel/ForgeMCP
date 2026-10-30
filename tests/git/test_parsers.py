"""Git machine records preserve file identities, complete metadata, and plain-text results."""

import pytest

from forgemcp.git import parsers
from forgemcp.git.errors import GitError
from forgemcp.git.models import (
    GitBlob,
    GitConflict,
    GitConflicts,
    GitDiff,
    GitHistory,
    GitStash,
    GitStashResult,
)
from forgemcp.git.text import render
from forgemcp.workspace.path import WorkspacePath


OID = b"1" * 40
OTHER_OID = b"2" * 40


def commit_record(message: bytes, oid: bytes = OID) -> bytes:
    """Encode the selected nine-field Git log format with an original commit message."""
    return b"\0".join(
        (
            oid,
            b"",
            "Автор".encode("utf-8"),
            b"author@example.test",
            b"2026-10-25T11:12:13+03:00",
            b"Committer",
            b"committer@example.test",
            b"2026-10-25T12:13:14+03:00",
            message,
        ),
    ) + b"\0"


def test_status_preserves_literal_names_renames_and_independent_states():
    """NUL framing keeps whitespace and Unicode paths distinct from status fields."""
    data = (
        b"# branch.oid " + OID + b"\0# branch.head feature/parser\0"
        b"# branch.upstream origin/main\0# branch.ab +3 -12\0"
        b"# unknown.future ignored\0"
        b"1 .M N... 100644 100644 100644 " + OID + b" " + OTHER_OID
        + b" src/with spaces.cpp\0"
        b"2 R. N... 100644 100644 100644 " + OID + b" " + OTHER_OID
        + b" R100 src/new\nname.cpp\0src/old\tname.cpp\0"
        b"? " + "src/новый.cpp".encode("utf-8") + b"\0! ignored.bin\0"
    )
    result = parsers.status(data)
    assert result.head == OID.decode()
    assert result.branch == "feature/parser"
    assert result.upstream == "origin/main"
    assert (result.ahead, result.behind) == (3, 12)
    changed, renamed, untracked, ignored = result.entries
    assert changed.path == "project/src/with spaces.cpp"
    assert (changed.index_status, changed.worktree_status) == (".", "M")
    assert renamed.path == "project/src/new\nname.cpp"
    assert renamed.original_path == "project/src/old\tname.cpp"
    assert untracked.path == "project/src/новый.cpp"
    assert untracked.index_status == "?"
    assert ignored.worktree_status == "!"
    assert "src/new\nname.cpp" in render(result)


def test_status_handles_unmerged_entries_detached_and_unborn_heads():
    """Conflict records retain both states and distinguish detached from unborn HEAD."""
    conflict = (
        b"# branch.oid " + OID + b"\0# branch.head (detached)\0"
        b"u UU S.M. 100644 100644 100644 100644 "
        + OID + b" " + OTHER_OID + b" " + OID + b" conflict with space.cpp\0"
    )
    result = parsers.status(conflict)
    assert result.branch is None
    assert result.head == OID.decode()
    assert result.entries[0].index_status == "U"
    assert result.entries[0].worktree_status == "U"
    assert result.entries[0].submodule == "S.M."
    unborn = parsers.status(b"# branch.oid (initial)\0# branch.head main\0")
    assert unborn.head is None and unborn.branch == "main"


@pytest.mark.parametrize(
    "data",
    (
        b"unknown record\0",
        b"1 .M N...\0",
        b"?missing separator\0",
        b"? \0",
        b"? ../escape.cpp\0",
        b"? invalid\xff.cpp\0",
        b"# branch.ab +invalid -2\0",
    ),
)
def test_status_rejects_malformed_or_unrepresentable_path_records(data):
    """Invalid records become correctable domain errors without lossy path replacement."""
    with pytest.raises(GitError):
        parsers.status(data)


def test_raw_diff_preserves_binary_names_modes_oids_and_rename_pairs():
    """Raw metadata records remain valid for binary files, symlinks, and SHA-256 objects."""
    sha256 = b"a" * 64
    other_sha256 = b"b" * 64
    data = (
        b":100644 100755 " + OID + b" " + OTHER_OID + b" M\0asset with space.bin\0"
        b":120000 120000 " + sha256 + b" " + other_sha256
        + b" R087\0old\nlink\0new\nlink\0"
    )
    files = parsers.changes(data)
    assert len(files) == 2
    binary = files[0]
    rename = files[1]
    assert binary.path == "project/asset with space.bin"
    assert (binary.old_mode, binary.new_mode) == ("100644", "100755")
    assert binary.old_oid == OID.decode() and binary.new_oid == OTHER_OID.decode()
    assert rename.path == "project/new\nlink"
    assert rename.original_path == "project/old\nlink"
    assert rename.status == "R087"
    assert rename.old_oid == sha256.decode() and rename.new_oid == other_sha256.decode()
    result = GitDiff(mode="unstaged", files=[binary], patch="")
    assert "asset with space.bin" in render(result)
    assert "100644 -> 100755" in render(result)


@pytest.mark.parametrize(
    "data",
    (
        b"not raw metadata\0name.cpp\0",
        b":100644 100644 1 2 R100\0old.cpp\0",
        b"::100644 100644 100644 1 2 3 MM\0merged.cpp\0",
    ),
)
def test_raw_diff_rejects_incomplete_or_combined_records(data):
    """Two-sided diff parsing refuses incomplete rename pairs and unsupported combined output."""
    with pytest.raises(GitError):
        parsers.changes(data)


def test_commit_log_preserves_multiline_messages_and_root_parent_lists():
    """Commit boundaries use NUL rather than message lines or punctuation."""
    message = "Subject α\n\nBody with tabs\tand a final newline.\n"
    records = parsers.commits(
        commit_record(message.encode("utf-8")) + commit_record(b"Second commit\n", OTHER_OID),
    )
    assert len(records) == 2
    assert records[0].parents == []
    assert records[0].author_name == "Автор"
    assert records[0].message == message
    assert records[1].oid == OTHER_OID.decode()
    history = GitHistory(revision=OID.decode(), commits=records, next_skip=2)
    text = render(history)
    assert message in text and "Second commit\n" in text
    assert "More commits: skip=2." in text
    with pytest.raises(GitError, match="commit records"):
        parsers.commits(b"missing\0fields\0")


def test_line_blame_keeps_original_lines_blank_text_and_sha256_identities():
    """Line porcelain attribution does not depend on human alignment or nonempty source text."""
    data = (
        OID + b" 40 2 1\nauthor Example Author\nauthor-mail <author@example.test>\n"
        b"summary first summary\nfilename file.cpp\n\tint value = 1;\n"
        + b"a" * 64 + b" 41 3 1\nauthor Other\nauthor-mail <other@example.test>\n"
        b"summary blank line\nfilename file.cpp\n\t\n"
    )
    lines = parsers.blame(data)
    assert (lines[0].original_line, lines[0].line) == (40, 2)
    assert lines[0].author_email == "author@example.test"
    assert lines[0].text == "int value = 1;"
    assert lines[1].oid == "a" * 64 and lines[1].text == ""
    with pytest.raises(GitError, match="source line"):
        parsers.blame(OID + b" 1 1 1\nauthor Incomplete\n")
    with pytest.raises(GitError, match="blame line"):
        parsers.blame(b"\tsource without attribution\n")


def test_conflict_text_distinguishes_absent_binary_and_empty_stages():
    """Conflict fallback preserves an empty textual stage and identifies binary stage objects."""
    result = GitConflicts(
        operation="merge",
        files=[
            GitConflict(
                path=WorkspacePath("project/conflict.bin"),
                ours=GitBlob(oid="1" * 40, mode="100644", binary=True, text=None),
                theirs=GitBlob(oid="2" * 40, mode="100644", binary=False, text=""),
            ),
        ],
    )
    text = render(result)
    assert "base: (absent)" in text
    assert "ours: (binary; oid=" + "1" * 40 in text
    assert "theirs: " in text


def test_stash_action_text_contains_records_and_command_outcome():
    """The fixed stash result schema retains both action state and list content in text."""
    result = GitStashResult(
        head_before="1" * 40,
        head_after="1" * 40,
        operation=None,
        conflicts=[],
        changes=[],
        process_ids=[7],
        stashes=[GitStash(reference="stash@{0}", oid="2" * 40, message="saved changes")],
    )
    text = render(result)
    assert "Result: success" in text and "Processes: 7" in text
    assert "stash@{0}" in text and "saved changes" in text
