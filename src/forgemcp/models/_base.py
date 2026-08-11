"""Shared validation helpers for transport-neutral domain models."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict


class ForgeModel(BaseModel):
    """Immutable base model with a strict, forward-compatible wire contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


def normalize_utc(value: datetime) -> datetime:
    """Reject naive timestamps and normalize valid instants to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include a UTC offset.")
    return value.astimezone(UTC)
