"""Typed, immutable line diffs for workspace mutation results."""

import difflib
from typing import Literal

from pydantic import BaseModel, Field

from .extensions import ExtensionContext, ExtensionOutput, ExtensionResource, TextChange
from .path import WorkspacePath


class DiffLine(BaseModel):
    kind: Literal["context", "added", "removed"]
    before_line: int | None = Field(ge=1)
    after_line: int | None = Field(ge=1)
    text: str


class DiffRange(BaseModel):
    kind: Literal["replace", "insert", "delete"]
    before_start: int = Field(ge=1)
    before_count: int = Field(ge=0)
    after_start: int = Field(ge=1)
    after_count: int = Field(ge=0)


class DiffHunk(BaseModel):
    before_start: int = Field(ge=1)
    before_count: int = Field(ge=0)
    after_start: int = Field(ge=1)
    after_count: int = Field(ge=0)
    lines: list[DiffLine]


class FileDiff(BaseModel):
    path: WorkspacePath
    changes: list[DiffRange]
    hunks: list[DiffHunk]


def create_diff(change: TextChange) -> FileDiff:
    """Preserve exact line text, including line endings and a missing final newline."""
    before = change.before.splitlines(keepends=True)
    after = change.after.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(
        None,
        before,
        after,
        autojunk=False,
    )
    changes = [
        DiffRange(
            kind=kind,
            before_start=i + 1,
            before_count=j - i,
            after_start=k + 1,
            after_count=l - k,
        )
        for kind, i, j, k, l in matcher.get_opcodes()
        if kind != "equal"
    ]
    hunks = []
    for group in matcher.get_grouped_opcodes(3):
        lines = []
        for kind, i, j, k, l in group:
            if kind == "equal":
                lines.extend(
                    DiffLine(
                        kind="context",
                        before_line=number + 1,
                        after_line=k + number - i + 1,
                        text=before[number],
                    )
                    for number in range(i, j)
                )
            else:
                lines.extend(
                    DiffLine(
                        kind="removed",
                        before_line=number + 1,
                        after_line=None,
                        text=before[number],
                    )
                    for number in range(i, j)
                )
                lines.extend(
                    DiffLine(
                        kind="added",
                        before_line=None,
                        after_line=number + 1,
                        text=after[number],
                    )
                    for number in range(k, l)
                )
        hunks.append(
            DiffHunk(
                before_start=group[0][1] + 1,
                before_count=group[-1][2] - group[0][1],
                after_start=group[0][3] + 1,
                after_count=group[-1][4] - group[0][3],
                lines=lines,
            )
        )
    return FileDiff(path=change.path, changes=changes, hunks=hunks)


async def diff_extension(context: ExtensionContext) -> ExtensionOutput | None:
    if context.change is None:
        return None
    diff = create_diff(context.change)
    return ExtensionOutput(
        data={"resource": "diff.json"},
        resources=(
            ExtensionResource(
                name="diff.json",
                mime_type="application/json",
                text=diff.model_dump_json(),
            ),
        ),
    )
