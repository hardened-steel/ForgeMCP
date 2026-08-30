"""Immutable, transport-neutral public models for Git Intelligence Phase 1."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import ConfigDict, Field, field_validator, model_validator

from forgemcp.models._base import ForgeModel, normalize_utc


MAX_GIT_STATUS_RECORDS = 512
MAX_GIT_PATHS = 64
MAX_GIT_PATCH_CHARACTERS = 65_536
MAX_GIT_COMMITS = 100
MAX_GIT_BRANCHES = 256
MAX_GIT_PARENTS = 16
MAX_GIT_BLAME_RANGES = 512
_OID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"


class GitRepositoryAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class GitFileStatus(ForgeModel):
    """One porcelain-v2 path record.  Path text is untrusted project data."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=False)

    path: str = Field(min_length=1, max_length=4096)
    staged_status: str = Field(min_length=1, max_length=1)
    unstaged_status: str = Field(min_length=1, max_length=1)
    untracked: bool = False
    conflicted: bool = False
    original_path: str | None = Field(default=None, min_length=1, max_length=4096)


class GitStatus(ForgeModel):
    """Bounded repository status; no Git/config/metadata filesystem path is exposed."""

    repository: GitRepositoryAvailability
    git_available: bool
    git_configured: bool
    branch: str | None = Field(default=None, min_length=1, max_length=1024)
    detached: bool = False
    unborn: bool = False
    head_oid: str | None = Field(default=None, pattern=_OID_PATTERN)
    ahead: int | None = Field(default=None, ge=0)
    behind: int | None = Field(default=None, ge=0)
    files: tuple[GitFileStatus, ...] = Field(default=(), max_length=MAX_GIT_STATUS_RECORDS)
    staged_count: int = Field(ge=0)
    unstaged_count: int = Field(ge=0)
    untracked_count: int = Field(ge=0)
    conflicted_count: int = Field(ge=0)
    incomplete: bool
    truncated: bool
    error: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def coherent(self) -> "GitStatus":
        if self.repository is GitRepositoryAvailability.AVAILABLE and not self.git_available:
            raise ValueError("An available repository requires Git availability.")
        if self.repository is GitRepositoryAvailability.AVAILABLE and self.error is not None:
            raise ValueError("An available repository cannot expose an error.")
        return self


class GitPatchSummary(ForgeModel):
    scope: str = Field(pattern=r"^(?:unstaged|staged)$")
    patch_truncated: bool
    binary_file_count: int = Field(ge=0)
    file_count: int = Field(ge=0)
    incomplete: bool


class GitDiffResult(ForgeModel):
    """An intentional bounded patch disclosure; never retained in logs/status."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=False)

    patch: str = Field(max_length=MAX_GIT_PATCH_CHARACTERS)
    summary: GitPatchSummary


class GitCommit(ForgeModel):
    """Normalized commit metadata.  Project-authored strings remain untrusted data."""

    oid: str = Field(pattern=_OID_PATTERN)
    parent_oids: tuple[str, ...] = Field(default=(), max_length=MAX_GIT_PARENTS)
    parents_truncated: bool = False
    subject: str = Field(min_length=1, max_length=1024)
    author_name: str = Field(min_length=1, max_length=512)
    authored_at: datetime

    @field_validator("authored_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return normalize_utc(value)


class GitLogResult(ForgeModel):
    commits: tuple[GitCommit, ...] = Field(max_length=MAX_GIT_COMMITS)
    next_cursor: str | None = Field(default=None, min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    truncated: bool
    incomplete: bool


class GitShowCommitResult(ForgeModel):
    # A patch is an intentional byte-derived disclosure; preserve its leading
    # and trailing whitespace exactly as Git produced it.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=False)

    commit: GitCommit
    patch: str = Field(max_length=MAX_GIT_PATCH_CHARACTERS)
    patch_truncated: bool
    binary_file_count: int = Field(ge=0)
    incomplete: bool


class GitBlameRange(ForgeModel):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    oid: str = Field(pattern=_OID_PATTERN)
    author_name: str = Field(min_length=1, max_length=512)
    authored_at: datetime

    @field_validator("authored_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        return normalize_utc(value)

    @model_validator(mode="after")
    def range_is_ordered(self) -> "GitBlameRange":
        if self.end_line < self.start_line:
            raise ValueError("Blame ranges must be ordered.")
        return self


class GitBlameResult(ForgeModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=False)

    path: str = Field(min_length=1, max_length=4096)
    ranges: tuple[GitBlameRange, ...] = Field(max_length=MAX_GIT_BLAME_RANGES)
    truncated: bool
    incomplete: bool


class GitBranch(ForgeModel):
    name: str = Field(min_length=1, max_length=1024)
    current: bool
    oid: str = Field(pattern=_OID_PATTERN)
    upstream: str | None = Field(default=None, min_length=1, max_length=1024)
    ahead: int | None = Field(default=None, ge=0)
    behind: int | None = Field(default=None, ge=0)


class GitBranchList(ForgeModel):
    branches: tuple[GitBranch, ...] = Field(max_length=MAX_GIT_BRANCHES)
    truncated: bool
    incomplete: bool
