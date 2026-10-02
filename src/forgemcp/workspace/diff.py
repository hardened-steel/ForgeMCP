"""Typed, immutable line diffs for workspace mutation results."""

import difflib
from typing import Literal

from pydantic import BaseModel, Field

from .extensions import ExtensionContext, ExtensionOutput, ExtensionResource, TextChange
from .path import WorkspacePath
from .service import FileEditResult, FileWriteResult, ResultProvider, WorkspaceService


class DiffLine(BaseModel):
    kind: Literal["context", "added", "removed"]
    before_line: int | None = Field(ge=1)
    after_line: int | None = Field(ge=1)
    text: str
    spans: list[tuple[int, int]] = Field(default_factory=list)


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
                before_spans = {}
                after_spans = {}
                if kind == "replace":
                    for old, new in zip(range(i, j), range(k, l)):
                        character_diff = difflib.SequenceMatcher(
                            None,
                            before[old],
                            after[new],
                            autojunk=False,
                        )
                        before_spans[old] = []
                        after_spans[new] = []
                        for operation, a, b, c, d in character_diff.get_opcodes():
                            if operation != "equal":
                                before_spans[old].append((a, b))
                                after_spans[new].append((c, d))
                lines.extend(
                    DiffLine(
                        kind="removed",
                        before_line=number + 1,
                        after_line=None,
                        text=before[number],
                        spans=before_spans.get(number, [(0, len(before[number]))]),
                    )
                    for number in range(i, j)
                )
                lines.extend(
                    DiffLine(
                        kind="added",
                        before_line=None,
                        after_line=number + 1,
                        text=after[number],
                        spans=after_spans.get(number, [(0, len(after[number]))]),
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
    """Keep the current Workspace handlers working until their provider migration."""
    if context.change is None:
        return None
    diff = create_diff(context.change)
    return ExtensionOutput(
        resource=ExtensionResource(
            name="diff.json",
            mime_type="application/json",
            text=diff.model_dump_json(),
        ),
        metadata={"version": 1},
    )


class DiffProvider(ResultProvider[str]):
    """Capture the old text before a write/edit and publish its immutable diff."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self.workspace = workspace

    async def before_workspace_write_file(
        self,
        call_id: str,
        path: WorkspacePath,
        text: str,
    ) -> str:
        candidate = self.workspace.writable_path(path.relative, root=path.area)
        return self.workspace.read_file(path).text if candidate.exists() else ""

    async def after_workspace_write_file(
        self,
        call_id: str,
        context: str,
        result: FileWriteResult,
    ) -> str:
        return self.save_diff(call_id, result.path, context)

    async def before_workspace_edit_file(
        self,
        call_id: str,
        path: WorkspacePath,
        old_text: str,
        new_text: str,
        replace_all: bool,
    ) -> str:
        return self.workspace.read_file(path).text

    async def after_workspace_edit_file(
        self,
        call_id: str,
        context: str,
        result: FileEditResult,
    ) -> str:
        return self.save_diff(call_id, result.path, context)

    def save_diff(self, call_id: str, path: WorkspacePath, before: str) -> str:
        after = self.workspace.read_file(path).text
        diff = create_diff(TextChange(path=path, before=before, after=after))
        return self.workspace.save_result_resource(
            call_id,
            "diff",
            "application/json",
            diff.model_dump_json(),
        )
