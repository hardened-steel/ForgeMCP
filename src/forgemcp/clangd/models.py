"""Language values shared by session decoding and service results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from forgemcp.workspace.path import WorkspacePath


class Position(BaseModel):
    """A one-based source line and zero-based Unicode character offset."""

    line: int = Field(ge=1)
    character: int = Field(ge=0, description="Zero-based Unicode code point offset.")


class SourceRange(BaseModel):
    """An ordered pair of source positions delimiting a range."""

    start: Position
    end: Position

    @model_validator(mode="after")
    def ordered(self) -> SourceRange:
        """Reject ranges whose end precedes the start."""
        if (self.end.line, self.end.character) < (self.start.line, self.start.character):
            raise ValueError("Source range end precedes its start.")
        return self


class Location(BaseModel):
    """A qualified file path and source range."""

    path: WorkspacePath
    range: SourceRange


class SourceExcerpt(BaseModel):
    """Immutable source context captured with a navigation answer."""

    start_line: int = Field(ge=1)
    text: str


class NavigationLocation(Location):
    """A navigation target with optional saved source context."""

    preview: SourceExcerpt | None = Field(
        default=None,
        description="Up to seven saved source lines near the target. External or unreadable files have no preview.",
    )


class RelatedDiagnostic(BaseModel):
    """A diagnostic note attached to another source location."""

    location: Location
    message: str


class Diagnostic(BaseModel):
    """A source diagnostic with severity, provenance, tags, and related notes."""

    range: SourceRange
    severity: Literal["error", "warning", "information", "hint"] | None = None
    message: str
    code: int | str | None = None
    source: str | None = None
    tags: list[int] = Field(default_factory=list)
    related: list[RelatedDiagnostic] = Field(default_factory=list)


class HoverText(BaseModel):
    """One hover fragment with its format and optional code language."""

    kind: Literal["plaintext", "markdown", "code"]
    text: str
    language: str | None = None


class Hover(BaseModel):
    """Hover fragments and the optional source range they describe."""

    contents: list[HoverText]
    range: SourceRange | None = None


class DocumentSymbol(BaseModel):
    """A hierarchical symbol with its full range and name selection range."""

    name: str
    kind: int
    range: SourceRange
    selection_range: SourceRange
    detail: str | None = None
    tags: list[int] = Field(default_factory=list)
    children: list[DocumentSymbol] = Field(default_factory=list)


class WorkspaceSymbol(BaseModel):
    """A workspace symbol with a navigation target and optional containing scope."""

    name: str
    kind: int
    location: NavigationLocation
    container_name: str | None = None
    tags: list[int] = Field(default_factory=list)


class HighlightSpan(BaseModel):
    """A semantic source range with its token kind and modifiers."""

    range: SourceRange
    kind: str
    modifiers: list[str] = Field(default_factory=list)


class DiagnosticsResult(BaseModel):
    """Diagnostics for one file with the configurations that produced them."""

    configurations: list[str]
    path: WorkspacePath
    diagnostics: list[Diagnostic]


class HoverResult(BaseModel):
    """A hover answer at a source position with configuration provenance."""

    configurations: list[str]
    path: WorkspacePath
    position: Position
    hover: Hover | None


class DefinitionResult(BaseModel):
    """Definition targets shared by the listed configurations."""

    configurations: list[str]
    locations: list[NavigationLocation]


class ReferencesResult(BaseModel):
    """Reference targets shared by the listed configurations."""

    configurations: list[str]
    locations: list[NavigationLocation]


class DocumentSymbolsResult(BaseModel):
    """A file's symbol tree with configuration provenance."""

    configurations: list[str]
    path: WorkspacePath
    symbols: list[DocumentSymbol]


class WorkspaceSymbolsResult(BaseModel):
    """Matching workspace symbols with configuration provenance."""

    configurations: list[str]
    symbols: list[WorkspaceSymbol]


class HighlightingResult(BaseModel):
    """Semantic spans for a file with configuration provenance."""

    configurations: list[str]
    path: WorkspacePath
    spans: list[HighlightSpan]


class FileAnalysis(BaseModel):
    """Saved diagnostic and highlighting answers for one file."""

    path: WorkspacePath
    diagnostics: list[DiagnosticsResult]
    highlighting: list[HighlightingResult]


class ClangdResource(BaseModel):
    """The versioned payload of an immutable workspace analysis resource."""

    version: Literal[1] = 1
    files: list[FileAnalysis]
