"""Transcript selections preserve stream order, line numbering and UTF-8 text."""

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.apps import Apps
from pydantic import TypeAdapter, ValidationError

from forgemcp.completion import Complete
from forgemcp.process.models import (
    FirstLines,
    LastLines,
    LineRange,
    ProcessStatus,
    ProcessSummary,
    ProcessTimeout,
    FirstSeconds,
    LastSeconds,
    TimeRange,
)
from forgemcp.process.service import ProcessRecord, ProcessService
from forgemcp.process.transcript import select_transcript


@pytest.fixture
def record():
    return ProcessRecord(
        summary=ProcessSummary(
            process_id=1,
            executable="compiler",
            arguments=[],
            cwd="project",
            encoding="utf-8",
            timeout=ProcessTimeout(),
        ),
        status=ProcessStatus(pid=1, current_status=0),
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


def select(record, *, lines=None, time=None, max_bytes=65536):
    return select_transcript(
        record.transcript,
        process_start=10,
        snapshot_end=14,
        lines=lines,
        time=time,
        max_bytes=max_bytes,
    )


def test_line_numbers_span_chunks_and_streams(record):
    record.append_log("stdout", "one\r", 10)
    record.append_log("stderr", "\ntwo\nthree", 11)
    record.append_log("stdin", " end\n", 12)
    record.append_log("stdout", "", 13)
    assert [(item.start_line, item.end_line) for item in record.transcript] == [
        (1, 1),
        (1, 3),
        (3, 3),
    ]
    assert record.next_line == 4
    selected = select(record, lines=LineRange(start=2, end=3))
    assert [item.text for item in selected] == ["two\nthree", " end\n"]
    assert [(item.start_line, item.end_line) for item in selected] == [(2, 3), (3, 3)]
    assert record.transcript[1].text == "\ntwo\nthree"


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (FirstLines(first=1), "one\n"),
        (LastLines(last=2), "two\nthree\n"),
        (LineRange(start=2, end=2), "two\n"),
        (LineRange(start=4, end=5), ""),
    ],
)
def test_distinct_lines_count_split_chunks_once(record, lines, expected):
    for text in ("on", "e\nt", "wo\nthree\n"):
        record.append_log("stdout", text, 10)
    assert "".join(item.text for item in select(record, lines=lines)) == expected


@pytest.mark.parametrize(
    ("time", "expected"),
    [
        (FirstSeconds(first=2), "a\nb\n"),
        (LastSeconds(last=2), "c\nd\n"),
        (TimeRange(start=1, end=3), "b\nc\n"),
        (TimeRange(start=5, end=6), ""),
    ],
)
def test_time_windows_use_chunk_timestamps_and_exclude_end(record, time, expected):
    for offset, text in enumerate("abcd"):
        record.append_log("stdout", text + "\n", 10 + offset)
    assert "".join(item.text for item in select(record, time=time)) == expected


def test_combined_limits_filter_time_then_lines_then_bytes(record):
    record.append_log("stdout", "old\n", 10)
    record.append_log("stderr", "alpha\nbeta\ngamma\n", 13)
    selected = select(
        record,
        time=LastSeconds(last=1),
        lines=LastLines(last=2),
        max_bytes=8,
    )
    assert [item.text for item in selected] == ["a\ngamma\n"]
    assert (selected[0].start_line, selected[-1].end_line) == (3, 4)
    assert selected[0].stream == "stderr"
    # Explicit first lines choose the head even within a trailing time window.
    selected = select(
        record,
        time=LastSeconds(last=1),
        lines=FirstLines(first=2),
        max_bytes=3,
    )
    assert selected[0].text == "alp"


@pytest.mark.parametrize(
    ("lines", "budget", "expected"),
    [
        (FirstLines(first=1), 4, "a"),
        (FirstLines(first=1), 5, "a😀"),
        (LastLines(last=1), 4, "я"),
        (LastLines(last=1), 6, "😀я"),
        (LastLines(last=1), 0, ""),
    ],
)
def test_byte_budget_never_splits_unicode_character(record, lines, budget, expected):
    record.append_log("stdout", "a😀я", 10)
    selected = select(record, lines=lines, max_bytes=budget)
    text = "".join(item.text for item in selected)
    assert text == expected
    assert len(text.encode("utf-8")) <= budget
    assert record.transcript[0].text == "a😀я"


def test_default_returns_last_100_lines_in_original_order(record):
    for number in range(1, 121):
        record.append_log("stdout", f"{number}\n", 10)
    selected = select(record)
    assert len(selected) == 100
    assert (selected[0].start_line, selected[-1].end_line) == (21, 120)
    assert "".join(item.text for item in selected) == "".join(
        f"{number}\n" for number in range(21, 121)
    )


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"first": 1, "last": 2},
        {"start": 1},
        {"start": 3, "end": 2},
        {"last": 0},
        {"first": 1, "unknown": 2},
    ],
)
def test_invalid_line_selectors(value):
    with pytest.raises(ValidationError):
        TypeAdapter(FirstLines | LastLines | LineRange).validate_python(value)


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"first": 1, "last": 2},
        {"end": 2},
        {"start": 1, "end": 1},
        {"last": 0},
        {"first": float("inf")},
        {"last": float("nan")},
    ],
)
def test_invalid_time_selectors(value):
    with pytest.raises(ValidationError):
        TypeAdapter(FirstSeconds | LastSeconds | TimeRange).validate_python(value)


@pytest.mark.parametrize(
    ("variant", "value"),
    [
        (FirstLines, {"first": 2}),
        (LastLines, {"last": 2}),
        (LineRange, {"start": 2, "end": 2}),
        (FirstSeconds, {"first": 0.5}),
        (LastSeconds, {"last": 0.5}),
        (TimeRange, {"start": 0, "end": 0.5}),
    ],
)
def test_selector_union_preserves_variant_and_json(variant, value):
    adapter = TypeAdapter(
        FirstLines | LastLines | LineRange
        if variant in (FirstLines, LastLines, LineRange)
        else FirstSeconds | LastSeconds | TimeRange
    )
    selected = adapter.validate_python(value)
    assert type(selected) is variant
    assert selected.model_dump() == value


@pytest.mark.anyio
@pytest.mark.parametrize("duration", [2, 3])
async def test_process_get_combines_limits_and_anchors_finished_tail(
    record,
    cpp_acceptance_project,
    duration,
):
    record.start = 10
    record.status.work_time = duration
    record.append_log("stdout", "old\n", 10)
    record.append_log("stderr", "recent\nlast\n", 13)
    processes = ProcessService(cpp_acceptance_project)
    processes.records[1] = record
    apps = Apps()
    server = MCPServer("transcript-test", extensions=[apps])
    processes.register(server, apps, Complete())
    for binding in apps.tools():
        server.add_tool(binding.fn, meta=binding.meta, **binding.kwargs)
    async with Client(server) as client:
        tool = next(item for item in (await client.list_tools()).tools if item.name == "process_get")
        schema = tool.input_schema
        for parameter, variants in (
            ("lines", ("FirstLines", "LastLines", "LineRange")),
            ("time", ("FirstSeconds", "LastSeconds", "TimeRange")),
        ):
            options = schema["properties"][parameter]["anyOf"]
            assert {item["$ref"].split("/")[-1] for item in options if "$ref" in item} == set(variants)
            for variant in variants:
                definition = schema["$defs"][variant]
                assert definition["additionalProperties"] is False
                assert set(definition["required"]) == set(definition["properties"])
        response = await client.call_tool(
            "process_get",
            {
                "process_id": 1,
                "time": {"last": 1},
                "lines": {"last": 1},
                "max_bytes": 4,
            },
        )
        assert not response.is_error
        result = response.structured_content
        assert set(result) == {"process", "transcript", "lines"}
        assert result["lines"] == [3, 3]
        assert [item["text"] for item in result["transcript"]] == ["ast\n"]
        empty = await client.call_tool("process_get", {"process_id": 1, "max_bytes": 0})
        assert empty.structured_content["lines"] is None
        assert empty.structured_content["transcript"] == []
        invalid = await client.call_tool(
            "process_get",
            {"process_id": 1, "lines": {"first": 1, "last": 1}},
        )
        assert invalid.is_error
    assert record.transcript[-1].text == "recent\nlast\n"
