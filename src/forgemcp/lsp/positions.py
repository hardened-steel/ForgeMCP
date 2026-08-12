"""Explicit conversion between ForgeMCP code-point coordinates and LSP units."""

from __future__ import annotations

from enum import StrEnum
from typing import Mapping

from forgemcp.lsp.errors import LspCoordinateError
from forgemcp.models import Position, Range


class PositionEncoding(StrEnum):
    """The LSP character encodings supported by this client."""

    UTF8 = "utf-8"
    UTF16 = "utf-16"
    UTF32 = "utf-32"


def line_at(text: str, line: int) -> str:
    """Return one logical LSP line, excluding its line terminator."""
    if not isinstance(line, int) or isinstance(line, bool) or line < 0:
        raise LspCoordinateError("Source positions must use non-negative line numbers.")
    lines = text.split("\n")
    if line >= len(lines):
        raise LspCoordinateError("The source position line is outside the current document.")
    return lines[line][:-1] if lines[line].endswith("\r") else lines[line]


def to_lsp_position(text: str, position: Position, encoding: PositionEncoding) -> dict[str, int]:
    """Convert a public code-point position into an LSP wire position."""
    source_line = line_at(text, position.line)
    if position.column > len(source_line):
        raise LspCoordinateError("The source position column is outside the current document line.")
    return {"line": position.line, "character": _unit_length(source_line[: position.column], encoding)}


def from_lsp_position(text: str, value: Mapping[str, object], encoding: PositionEncoding) -> Position:
    """Convert an LSP wire position, rejecting indices within a surrogate sequence."""
    line = value.get("line")
    character = value.get("character")
    if (
        not isinstance(line, int)
        or isinstance(line, bool)
        or line < 0
        or not isinstance(character, int)
        or isinstance(character, bool)
        or character < 0
    ):
        raise LspCoordinateError("The language server returned an invalid source position.")
    source_line = line_at(text, line)
    consumed = 0
    for column, character_text in enumerate(source_line):
        if consumed == character:
            return Position(line=line, column=column)
        consumed += _unit_length(character_text, encoding)
        if consumed > character:
            raise LspCoordinateError("The language server position splits an encoded character.")
    if consumed == character:
        return Position(line=line, column=len(source_line))
    raise LspCoordinateError("The language server position is outside the current document line.")


def from_lsp_range(text: str, value: Mapping[str, object], encoding: PositionEncoding) -> Range:
    """Convert one LSP range to ForgeMCP's public code-point range."""
    start = value.get("start")
    end = value.get("end")
    if not isinstance(start, Mapping) or not isinstance(end, Mapping):
        raise LspCoordinateError("The language server returned an invalid source range.")
    return Range(start=from_lsp_position(text, start, encoding), end=from_lsp_position(text, end, encoding))


def to_lsp_range(text: str, value: Range, encoding: PositionEncoding) -> dict[str, object]:
    """Convert one public code-point range to its LSP wire representation."""
    return {
        "start": to_lsp_position(text, value.start, encoding),
        "end": to_lsp_position(text, value.end, encoding),
    }


def _unit_length(value: str, encoding: PositionEncoding) -> int:
    if encoding is PositionEncoding.UTF32:
        return len(value)
    if encoding is PositionEncoding.UTF8:
        return len(value.encode("utf-8"))
    return len(value.encode("utf-16-le")) // 2
