"""Language values shared by session decoding and service results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from forgemcp.workspace.path import WorkspacePath


class Position(BaseModel):
    line: int = Field(ge=1)
    character: int = Field(ge=0, description="Zero-based Unicode code point offset.")


class SourceRange(BaseModel):
    start: Position
    end: Position

    @model_validator(mode="after")
    def ordered(self) -> SourceRange:
        if (self.end.line, self.end.character) < (self.start.line, self.start.character):
            raise ValueError("Source range end precedes its start.")
        return self


class Location(BaseModel):
    path: WorkspacePath
    range: SourceRange


class RelatedDiagnostic(BaseModel):
    location: Location
    message: str


class Diagnostic(BaseModel):
    range: SourceRange
    severity: Literal["error", "warning", "information", "hint"] | None = None
    message: str
    code: int | str | None = None
    source: str | None = None
    tags: list[int] = Field(default_factory=list)
    related: list[RelatedDiagnostic] = Field(default_factory=list)


class HoverText(BaseModel):
    kind: Literal["plaintext", "markdown", "code"]
    text: str
    language: str | None = None


class Hover(BaseModel):
    contents: list[HoverText]
    range: SourceRange | None = None


class DocumentSymbol(BaseModel):
    name: str
    kind: int
    range: SourceRange
    selection_range: SourceRange
    detail: str | None = None
    tags: list[int] = Field(default_factory=list)
    children: list[DocumentSymbol] = Field(default_factory=list)


class WorkspaceSymbol(BaseModel):
    name: str
    kind: int
    location: Location
    container_name: str | None = None
    tags: list[int] = Field(default_factory=list)


class HighlightSpan(BaseModel):
    range: SourceRange
    kind: str
    modifiers: list[str] = Field(default_factory=list)
