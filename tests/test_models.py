from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from forgemcp.models import (
    MAX_PROCESS_OUTPUT_CHARACTERS,
    Diagnostic,
    FileChange,
    FileChangeKind,
    FileSnapshot,
    Location,
    PatchResult,
    Position,
    ProcessOutput,
    ProcessResult,
    Range,
    Severity,
    TaskResult,
    TaskState,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
FILE_URI = "file:///workspace/src/main.cpp"


def snapshot(*, exists: bool = True, captured_at: datetime = NOW) -> FileSnapshot:
    """Create a valid content-free snapshot for tests."""
    return FileSnapshot(
        uri=FILE_URI,
        exists=exists,
        size_bytes=14 if exists else None,
        sha256="a" * 64 if exists else None,
        modified_at=NOW if exists else None,
        captured_at=captured_at,
    )


def test_diagnostic_serializes_to_transport_neutral_json():
    diagnostic = Diagnostic(
        message="Unused variable.",
        severity=Severity.WARNING,
        location=Location(
            uri=FILE_URI,
            range=Range(start=Position(line=2, column=4), end=Position(line=2, column=12)),
        ),
        code="unused-variable",
        source="clangd",
    )

    assert diagnostic.model_dump(mode="json") == {
        "message": "Unused variable.",
        "severity": "warning",
        "location": {
            "uri": FILE_URI,
            "range": {
                "start": {"line": 2, "column": 4},
                "end": {"line": 2, "column": 12},
            },
        },
        "code": "unused-variable",
        "source": "clangd",
    }
    assert Diagnostic.model_validate_json(diagnostic.model_dump_json()) == diagnostic


def test_range_rejects_reverse_source_order_and_unknown_fields():
    with pytest.raises(ValidationError, match="Range end must not precede"):
        Range(start=Position(line=3, column=0), end=Position(line=2, column=10))

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Position(line=0, column=0, encoding="utf-16")


def test_task_result_normalizes_offset_timestamps_and_requires_terminal_state():
    started = datetime(2026, 8, 11, 15, 0, tzinfo=timezone(timedelta(hours=3)))
    result = TaskResult(
        task_id="build:42",
        state=TaskState.SUCCEEDED,
        started_at=started,
        finished_at=NOW,
    )

    assert result.started_at == NOW
    assert result.model_dump(mode="json")["started_at"] == "2026-08-11T12:00:00Z"

    with pytest.raises(ValidationError, match="TaskResult requires a terminal"):
        TaskResult(
            task_id="build:43",
            state=TaskState.RUNNING,
            started_at=NOW,
            finished_at=NOW,
        )
    with pytest.raises(ValidationError, match="Timestamp must include a UTC offset"):
        TaskResult(
            task_id="build:44",
            state=TaskState.FAILED,
            started_at=datetime(2026, 8, 11, 12, 0),
            finished_at=NOW,
        )


def test_process_result_bounds_output_and_hides_text_from_log_summary():
    output = ProcessOutput(text="compiler output", truncated=True)
    result = ProcessResult(
        exit_code=1,
        started_at=NOW,
        finished_at=NOW,
        stdout=output,
        stderr=ProcessOutput(text="error"),
    )

    assert result.model_dump(mode="json")["stdout"] == {
        "text": "compiler output",
        "truncated": True,
    }
    assert output.log_summary() == {"characters": 15, "truncated": True}

    assert ProcessOutput(text=" compiler output\n").text == " compiler output\n"

    with pytest.raises(ValidationError):
        ProcessOutput(text="x" * (MAX_PROCESS_OUTPUT_CHARACTERS + 1))
    with pytest.raises(ValidationError, match="Timed-out processes must not expose an exit code"):
        ProcessResult(
            exit_code=124,
            timed_out=True,
            started_at=NOW,
            finished_at=NOW,
            stdout=ProcessOutput(text=""),
            stderr=ProcessOutput(text=""),
        )


def test_file_models_are_content_free_and_enforce_atomic_patch_semantics():
    before = snapshot(captured_at=NOW)
    after = snapshot(captured_at=NOW + timedelta(seconds=1))
    change = FileChange(uri=FILE_URI, kind=FileChangeKind.MODIFIED, before=before, after=after)
    result = PatchResult(applied=True, changes=(change,))

    encoded = result.model_dump(mode="json")
    assert encoded["changes"][0]["before"] == {
        "uri": FILE_URI,
        "exists": True,
        "size_bytes": 14,
        "sha256": "a" * 64,
        "modified_at": "2026-08-11T12:00:00Z",
        "captured_at": "2026-08-11T12:00:00Z",
    }
    assert "content" not in encoded["changes"][0]["before"]

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FileSnapshot(
            uri=FILE_URI,
            exists=True,
            size_bytes=14,
            captured_at=NOW,
            content="int main() {}",
        )
    with pytest.raises(ValidationError, match="Missing file snapshots cannot include"):
        FileSnapshot(uri=FILE_URI, exists=False, size_bytes=0, captured_at=NOW)
    with pytest.raises(ValidationError, match="A failed atomic patch cannot report"):
        PatchResult(applied=False, changes=(change,))
