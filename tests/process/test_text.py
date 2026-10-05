"""Readable process commands and exact selected transcript fragments."""

import os
from datetime import datetime, timezone

import pytest

from forgemcp.process.models import (
    ProcessDetails,
    ProcessInfo,
    ProcessLogEntry,
    ProcessOverview,
    ProcessStatus,
    ProcessSummary,
    ProcessTimeout,
)
from forgemcp.process.text import describe_status, render_details, render_overview, render_process


def sample_process() -> ProcessInfo:
    """Make a completed command with arguments requiring shell-style display quoting."""
    return ProcessInfo(
        summary=ProcessSummary(
            process_id=7,
            executable="compiler with spaces",
            arguments=["input file.cpp", ""],
            cwd="project/",
            encoding="utf-8",
            timeout=ProcessTimeout(total=10),
        ),
        status=ProcessStatus(
            pid=42,
            started=datetime(2026, 10, 5, 12, tzinfo=timezone.utc),
            work_time=0.123456,
            current_status=0,
        ),
    )


def test_command_precedes_metadata_and_preserves_argument_boundaries():
    """Show executable and arguments together, quoting spaces and empty arguments."""
    text = render_process(sample_process())
    expected = (
        '"compiler with spaces" "input file.cpp" ""'
        if os.name == "nt"
        else "'compiler with spaces' 'input file.cpp' ''"
    )
    assert text.splitlines()[0] == expected
    assert "Process: 7; PID: 42" in text
    assert "Work time: 0.123 s" in text
    assert "Total timeout: 10.0 s" in text
    assert "Idle timeout: None" in text


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (0, "Completed successfully"),
        (2, "Exited with code 2"),
        ("running", "Running"),
        ("interrupted", "Interrupted by timeout"),
        ("stopped", "Stopped"),
        ("stream_failure", "Stream failure"),
    ],
)
def test_lifecycle_outcomes_are_explicit(state, expected):
    """Distinguish nonzero exits, timeout interruptions, stops, and stream failures."""
    assert describe_status(state) == expected


def test_overview_keeps_global_counts_distinct_from_filtered_processes():
    """Avoid describing global running/completed totals as sizes of the returned filter."""
    overview = ProcessOverview(running=3, completed=4, processes=[sample_process()])
    text = render_overview(overview, "completed")
    assert text.startswith("3 running processes; 4 completed. Showing 1 processes")
    assert "filter: completed" in text


def test_selected_transcript_keeps_order_streams_and_partial_lines():
    """Retain unmodified fragment text and overlapping absolute line provenance."""
    process = sample_process()
    entries = [
        ProcessLogEntry(
            timestamp=process.status.started,
            time=1.0,
            stream="stdout",
            text="partial",
            start_line=17,
            end_line=17,
        ),
        ProcessLogEntry(
            timestamp=process.status.started,
            time=2.0,
            stream="stderr",
            text="α\n",
            start_line=17,
            end_line=17,
        ),
    ]
    text = render_details(ProcessDetails(process=process, transcript=entries, lines=(17, 17)))
    assert "Returned transcript lines 17–17" in text
    assert text.index("stdout") < text.index("stderr")
    assert "(lines 17–17)\npartial" in text
    assert text.endswith("α\n")
    assert "Full transcript" not in text
    empty = render_details(ProcessDetails(process=process, transcript=[]))
    assert empty.endswith("No transcript entries in the selected range.")
