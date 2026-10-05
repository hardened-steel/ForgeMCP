"""Plain-text commands, lifecycle snapshots, and selected process transcripts."""

import os
import shlex
import subprocess

from forgemcp.text import timestamp

from .models import ProcessDetails, ProcessInfo, ProcessOverview


def describe_status(status: str | int) -> str:
    """Translate tracked states and exit codes into readable outcomes."""
    if isinstance(status, int):
        return "Completed successfully" if status == 0 else f"Exited with code {status}"
    return {
        "running": "Running",
        "interrupted": "Interrupted by timeout",
        "stopped": "Stopped",
        "stream_failure": "Stream failure",
    }[status]


def render_process(info: ProcessInfo) -> str:
    """Show the command first, followed by its identity and lifecycle metadata."""
    summary = info.summary
    status = info.status
    arguments = [summary.executable, *summary.arguments]
    command = subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
    total_timeout = f"{summary.timeout.total} s" if summary.timeout.total is not None else "None"
    idle_timeout = f"{summary.timeout.idle} s" if summary.timeout.idle is not None else "None"
    return "\n".join(
        [
            command,
            f"  Process: {summary.process_id}; PID: {status.pid}",
            f"  Outcome: {describe_status(status.current_status)}",
            f"  Working directory: {summary.cwd}",
            f"  Started: {timestamp(status.started)}",
            f"  Work time: {status.work_time:.3f} s",
            f"  Encoding: {summary.encoding}",
            f"  Total timeout: {total_timeout}",
            f"  Idle timeout: {idle_timeout}",
        ]
    )


def render_overview(result: ProcessOverview, selected_status: str) -> str:
    """Separate global counts from the filtered list of process snapshots."""
    summary = (
        f"{result.running} running processes; {result.completed} completed. "
        f"Showing {len(result.processes)} processes (filter: {selected_status})."
    )
    return "\n\n".join([summary, *(render_process(info) for info in result.processes)])


def render_details(result: ProcessDetails) -> str:
    """Present only the selected transcript fragments with their stream and line provenance."""
    blocks = [render_process(result.process)]
    if result.lines is None:
        blocks.append("No transcript entries in the selected range.")
    else:
        blocks.append(f"Returned transcript lines {result.lines[0]}–{result.lines[1]}:")
    for entry in result.transcript:
        blocks.append(
            f"{entry.stream} — {timestamp(entry.timestamp)} "
            f"(lines {entry.start_line}–{entry.end_line})\n{entry.text}"
        )
    return "\n\n".join(blocks)
