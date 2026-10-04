"""Select transcript fragments without changing retained log entries."""

from dataclasses import replace
from collections.abc import Sequence

from .models import (
    FirstLines,
    LastLines,
    LineRange,
    FirstSeconds,
    LastSeconds,
    TimeRange,
    ProcessLogEntry,
)


def fragment(entry: ProcessLogEntry, start: int, end: int) -> ProcessLogEntry:
    """Copy a text slice with recalculated absolute line bounds and unchanged provenance."""
    text = entry.text[start:end]
    first = entry.start_line + entry.text.count("\n", 0, start)
    return replace(
        entry,
        text=text,
        start_line=first,
        end_line=first + text.count("\n") - int(text.endswith("\n")),
    )


def line_offset(text: str, count: int) -> int:
    """Return the character offset after a requested number of LF-terminated lines."""
    offset = 0
    for _ in range(count):
        index = text.find("\n", offset)
        if index == -1:
            return len(text)
        offset = index + 1
    return offset


def select_transcript(
    entries: Sequence[ProcessLogEntry],
    *,
    process_start: float,
    snapshot_end: float,
    lines: FirstLines | LastLines | LineRange | None,
    time: FirstSeconds | LastSeconds | TimeRange | None,
    max_bytes: int,
) -> list[ProcessLogEntry]:
    """Apply time, distinct line selection, then a UTF-8 text byte budget."""
    if lines is None and time is None:
        lines = LastLines(last=100)
    lower_time = process_start
    upper_time = None
    if time is not None:
        if isinstance(time, FirstSeconds):
            upper_time = process_start + time.first
        elif isinstance(time, LastSeconds):
            lower_time = max(process_start, snapshot_end - time.last)
        else:
            lower_time = process_start + time.start
            upper_time = process_start + time.end
    selected = [
        entry
        for entry in entries
        if entry.text and entry.time >= lower_time
        and (upper_time is None or entry.time < upper_time)
    ]
    if not selected or max_bytes == 0:
        return []
    first = selected[0].start_line
    last = selected[-1].end_line
    from_end = isinstance(lines, LastLines) if lines is not None else isinstance(time, LastSeconds)
    if lines is not None:
        if isinstance(lines, LineRange):
            first, last = lines.start, lines.end
        else:
            # Merge overlapping entry ranges, so split lines count only once.
            ranges = []
            for entry in selected:
                if ranges and entry.start_line <= ranges[-1][1] + 1:
                    ranges[-1][1] = max(ranges[-1][1], entry.end_line)
                else:
                    ranges.append([entry.start_line, entry.end_line])
            remaining = lines.last if isinstance(lines, LastLines) else lines.first
            for begin, end in reversed(ranges) if from_end else ranges:
                size = end - begin + 1
                if remaining <= size:
                    if from_end:
                        first = end - remaining + 1
                    else:
                        last = begin + remaining - 1
                    break
                remaining -= size
    result = []
    remaining_bytes = max_bytes
    for entry in reversed(selected) if from_end else selected:
        if entry.end_line < first or entry.start_line > last:
            continue
        start = line_offset(entry.text, max(0, first - entry.start_line))
        end = line_offset(entry.text, last - entry.start_line + 1)
        if start == end:
            continue
        part = fragment(entry, start, end)
        encoded = part.text.encode("utf-8")
        if len(encoded) > remaining_bytes:
            data = encoded[-remaining_bytes:] if from_end else encoded[:remaining_bytes]
            text = data.decode("utf-8", errors="ignore")
            if text:
                offset = len(part.text) - len(text) if from_end else 0
                result.append(fragment(part, offset, offset + len(text)))
            break
        result.append(part)
        remaining_bytes -= len(encoded)
        if remaining_bytes == 0:
            break
    if from_end:
        result.reverse()
    return result
