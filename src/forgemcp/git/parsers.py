"""Decode bounded Git machine formats without interpreting project text as commands."""

import re

from forgemcp.workspace.path import WorkspacePath

from .errors import GitParseError
from .models import GitBlameLine, GitChange, GitCommit, GitEntry, GitStatus


def utf8(value: bytes) -> str:
    """Reject non-UTF-8 machine fields rather than silently changing file identities."""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GitParseError("Git returned a non-UTF-8 name or metadata field.") from error


def qualified(value: bytes) -> WorkspacePath:
    """Qualify a literal repository path while preserving whitespace and Unicode."""
    if not value:
        raise GitParseError("Git returned an empty repository file path.")
    try:
        return WorkspacePath("project/" + utf8(value))
    except ValueError as error:
        raise GitParseError("Git returned a noncanonical project path.") from error


def status(data: bytes) -> GitStatus:
    """Parse porcelain v2 NUL records, including rename pairs and unmerged entries."""
    headers: dict[str, str | int | None] = {}
    entries: list[GitEntry] = []
    records = iter(data.split(b"\0"))
    try:
        for record in records:
            if not record:
                continue
            if record.startswith(b"# "):
                key, _, raw = record[2:].partition(b" ")
                value = utf8(raw)
                if key == b"branch.oid":
                    headers["head"] = None if value == "(initial)" else value
                elif key == b"branch.head":
                    headers["branch"] = None if value == "(detached)" else value
                elif key == b"branch.upstream":
                    headers["upstream"] = value
                elif key == b"branch.ab":
                    ahead, behind = value.split(" ")
                    headers["ahead"], headers["behind"] = int(ahead), -int(behind)
                continue
            marker = record[:1]
            if marker in (b"?", b"!"):
                if not record.startswith(marker + b" ") or not record[2:]:
                    raise ValueError("Missing literal status path.")
                entries.append(
                    GitEntry(
                        path=qualified(record[2:]),
                        index_status=utf8(marker),
                        worktree_status=utf8(marker),
                    )
                )
                continue
            fields = record.split(b" ", {b"1": 8, b"2": 9, b"u": 10}[marker])
            if len(fields) != {b"1": 9, b"2": 10, b"u": 11}[marker] or not fields[-1]:
                raise ValueError("Incomplete tracked status entry.")
            state = utf8(fields[1])
            if len(state) != 2:
                raise ValueError("Invalid index/worktree status pair.")
            entries.append(
                GitEntry(
                    path=qualified(fields[-1]),
                    original_path=qualified(next(records)) if marker == b"2" else None,
                    index_status=state[0],
                    worktree_status=state[1],
                    submodule=utf8(fields[2]),
                )
            )
    except (KeyError, IndexError, ValueError, StopIteration) as error:
        raise GitParseError("Cannot parse Git porcelain v2 status.") from error
    return GitStatus(entries=entries, **headers)


def changes(data: bytes) -> list[GitChange]:
    """Parse raw NUL diff records with complete mode, OID, and rename information."""
    result: list[GitChange] = []
    records = iter(data.split(b"\0"))
    try:
        for record in records:
            if not record:
                continue
            if not record.startswith(b":") or record.startswith(b"::"):
                raise ValueError("Expected a two-sided raw diff.")
            old_mode, new_mode, old_oid, new_oid, state = utf8(record).split(" ")
            original = qualified(next(records))
            renamed = state.startswith(("R", "C"))
            result.append(
                GitChange(
                    path=qualified(next(records)) if renamed else original,
                    original_path=original if renamed else None,
                    status=state,
                    old_mode=old_mode.removeprefix(":"),
                    new_mode=new_mode,
                    old_oid=old_oid,
                    new_oid=new_oid,
                )
            )
    except (ValueError, StopIteration) as error:
        raise GitParseError("Cannot parse Git raw diff records.") from error
    return result


def commits(data: bytes) -> list[GitCommit]:
    """Decode nine NUL-separated fields per commit, preserving multiline messages."""
    if not data:
        return []
    fields = data.split(b"\0")
    if fields[-1] == b"":
        fields.pop()
    if len(fields) % 9:
        raise GitParseError("Cannot parse Git commit records.")
    result: list[GitCommit] = []
    for offset in range(0, len(fields), 9):
        values = [utf8(value) for value in fields[offset:offset + 9]]
        result.append(
            GitCommit(
                oid=values[0],
                parents=values[1].split(),
                author_name=values[2],
                author_email=values[3],
                author_time=values[4],
                committer_name=values[5],
                committer_email=values[6],
                committer_time=values[7],
                message=values[8],
            )
        )
    return result


def blame(data: bytes) -> list[GitBlameLine]:
    """Parse line porcelain without relying on human-readable blame alignment."""
    result: list[GitBlameLine] = []
    current: dict[str, str] = {}
    for raw in data.split(b"\n"):
        if raw.startswith(b"\t"):
            try:
                result.append(
                    GitBlameLine(
                        oid=current["oid"],
                        original_line=int(current["original"]),
                        line=int(current["line"]),
                        author=current.get("author", ""),
                        author_email=current.get("author-mail", "").strip("<>"),
                        summary=current.get("summary", ""),
                        text=utf8(raw[1:]),
                    )
                )
            except (KeyError, ValueError) as error:
                raise GitParseError("Cannot parse Git blame line.") from error
            current = {}
        elif match := re.fullmatch(
            rb"([0-9a-f]{40}(?:[0-9a-f]{24})?) ([0-9]+) ([0-9]+)(?: [0-9]+)?",
            raw,
        ):
            if current:
                raise GitParseError("Git blame ended a record without a source line.")
            current = dict(zip(("oid", "original", "line"), map(utf8, match.groups())))
        elif raw:
            key, _, value = raw.partition(b" ")
            current[utf8(key)] = utf8(value)
    if current:
        raise GitParseError("Git blame ended a record without a source line.")
    return result
