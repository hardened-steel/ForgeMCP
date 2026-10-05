"""Typed workspace results shared by operations and plain-text presentation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from .path import WorkspacePath
from .providers import ResultResources


class TreeEntry(BaseModel):
    """A qualified filesystem entry with its kind and optional expanded children."""

    path: WorkspacePath
    kind: Literal["file", "directory", "symlink", "other"]
    children: list[TreeEntry] | None = None


class DirectoryTree(ResultResources):
    """A requested directory and its tree entries with linked provider resources."""

    path: WorkspacePath
    entries: list[TreeEntry]


class FoundFile(BaseModel):
    """One matching file with its path, modification time, and size."""

    path: WorkspacePath
    modified_at: datetime
    size_bytes: int


class FilePaths(ResultResources):
    """Matched qualified paths and file metadata with linked provider resources."""

    paths: list[WorkspacePath]
    files: list[FoundFile] = Field(default_factory=list)


class FileContent(ResultResources):
    """A UTF-8 file excerpt with its qualified path and first source line."""

    path: WorkspacePath
    text: str
    start_line: int


class FileInfo(ResultResources):
    """File timestamps, size, and optional owner with linked provider resources."""

    path: WorkspacePath
    created_at: datetime | None
    modified_at: datetime
    size_bytes: int
    owner: str | None


class SearchMatch(BaseModel):
    """One matching source line with zero-based Unicode match spans."""

    path: WorkspacePath
    line: int
    text: str
    spans: list[tuple[int, int]] = Field(
        description="Match ranges as zero-based Unicode code point [start, end) pairs.",
    )


class SearchResult(ResultResources):
    """Matching source lines and skipped files with linked provider resources."""

    matches: list[SearchMatch]
    skipped_files: list[WorkspacePath]


class FileWriteResult(ResultResources):
    """A created or overwritten file with removed and added line counts."""

    path: WorkspacePath
    action: Literal["created", "overwritten"]
    lines_removed: int
    lines_added: int


class FileEditResult(ResultResources):
    """An edited file and the number of exact replacements performed."""

    path: WorkspacePath
    replacements: int


class PathOperationResult(ResultResources):
    """A path mutation outcome with an optional original move source."""

    path: WorkspacePath
    action: Literal["moved", "deleted", "created", "already_exists"]
    source: WorkspacePath | None = None
