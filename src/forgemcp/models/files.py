"""Content-free file metadata and patch outcome models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from forgemcp.models._base import ForgeModel, normalize_utc


class FileChangeKind(StrEnum):
    """The observable effect of an applied file change."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


class FileSnapshot(ForgeModel):
    """Content-free metadata for one resource at a captured UTC instant."""

    uri: str = Field(
        min_length=1,
        max_length=4096,
        description="Opaque resource URI of the snapshotted file.",
    )
    exists: bool = Field(description="Whether the resource existed at capture time.")
    size_bytes: int | None = Field(
        default=None,
        ge=0,
        description="File size in bytes when the resource existed; never file content.",
    )
    sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Optional lowercase SHA-256 digest when the resource existed; never file content.",
    )
    modified_at: datetime | None = Field(
        default=None,
        description="Optional UTC filesystem modification instant when the resource existed.",
    )
    captured_at: datetime = Field(description="UTC instant at which this metadata was observed.")

    @field_validator("modified_at", "captured_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: datetime | None) -> datetime | None:
        """Store optional filesystem timestamps as UTC-aware datetimes."""
        return None if value is None else normalize_utc(value)

    @model_validator(mode="after")
    def metadata_must_match_existence(self) -> "FileSnapshot":
        """Prevent metadata for absent files and require a size for present files."""
        metadata = (self.size_bytes, self.sha256, self.modified_at)
        if self.exists and self.size_bytes is None:
            raise ValueError("Existing file snapshots require size_bytes.")
        if not self.exists and any(value is not None for value in metadata):
            raise ValueError("Missing file snapshots cannot include file metadata.")
        return self


class FileChange(ForgeModel):
    """A content-free before/after record for one created, modified, or deleted file."""

    uri: str = Field(
        min_length=1,
        max_length=4096,
        description="Opaque resource URI of the changed file.",
    )
    kind: FileChangeKind = Field(description="Created, modified, or deleted effect.")
    before: FileSnapshot | None = Field(
        default=None,
        description="Content-free snapshot before the change, if the file existed.",
    )
    after: FileSnapshot | None = Field(
        default=None,
        description="Content-free snapshot after the change, if the file exists.",
    )

    @model_validator(mode="after")
    def snapshots_must_match_the_declared_change(self) -> "FileChange":
        """Ensure snapshots agree on URI and creation/modification/deletion semantics."""
        if self.before is not None and self.before.uri != self.uri:
            raise ValueError("FileChange before URI must match uri.")
        if self.after is not None and self.after.uri != self.uri:
            raise ValueError("FileChange after URI must match uri.")
        if self.kind is FileChangeKind.CREATED:
            valid = self.before is None and self.after is not None and self.after.exists
        elif self.kind is FileChangeKind.MODIFIED:
            valid = (
                self.before is not None
                and self.before.exists
                and self.after is not None
                and self.after.exists
            )
        else:
            valid = self.before is not None and self.before.exists and self.after is None
        if not valid:
            raise ValueError("FileChange snapshots do not match its declared kind.")
        return self


class PatchResult(ForgeModel):
    """Atomic, content-free report of a requested patch operation."""

    applied: bool = Field(description="Whether every requested file change was applied.")
    changes: tuple[FileChange, ...] = Field(
        default=(),
        max_length=10_000,
        description="Content-free file changes, present only after a successful atomic patch.",
    )

    @model_validator(mode="after")
    def failed_patches_must_not_report_changes(self) -> "PatchResult":
        """Keep failure reports atomic: no change is reported unless all were applied."""
        if not self.applied and self.changes:
            raise ValueError("A failed atomic patch cannot report applied changes.")
        return self
