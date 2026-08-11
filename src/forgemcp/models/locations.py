"""Source-coordinate models shared by diagnostics and tooling adapters."""

from __future__ import annotations

from pydantic import Field, model_validator

from forgemcp.models._base import ForgeModel


class Position(ForgeModel):
    """A zero-based source coordinate using Unicode code-point columns."""

    line: int = Field(ge=0, description="Zero-based line number in the resource.")
    column: int = Field(ge=0, description="Zero-based Unicode code-point column in the line.")

    def as_tuple(self) -> tuple[int, int]:
        """Return the position in lexical source order."""
        return (self.line, self.column)


class Range(ForgeModel):
    """A half-open source interval from ``start`` up to, but excluding, ``end``."""

    start: Position = Field(description="Inclusive start position of the range.")
    end: Position = Field(description="Exclusive end position of the range.")

    @model_validator(mode="after")
    def end_must_not_precede_start(self) -> "Range":
        """Ensure a range can be traversed in source order."""
        if self.end.as_tuple() < self.start.as_tuple():
            raise ValueError("Range end must not precede its start.")
        return self


class Location(ForgeModel):
    """A source range identified by an opaque, non-empty resource URI."""

    uri: str = Field(
        min_length=1,
        max_length=4096,
        description="Opaque resource URI; adapters define how their native paths map to it.",
    )
    range: Range = Field(description="Source range within the identified resource.")
