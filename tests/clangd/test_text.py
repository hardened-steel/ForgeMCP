"""Plain-text language answers, preserving code and configuration provenance."""

import pytest

from forgemcp.clangd.models import (
    Diagnostic,
    DiagnosticsResult,
    DocumentSymbol,
    Hover,
    HoverText,
    NavigationLocation,
    Position,
    RelatedDiagnostic,
    SourceRange,
)
from forgemcp.clangd.text import (
    diagnostic_text,
    document_symbols_text,
    hover_text,
    location_text,
    markdown_text,
    render_answers,
)
from forgemcp.workspace.path import WorkspacePath


def sample_range() -> SourceRange:
    """Exercise one-based lines and zero-based Unicode character positions."""
    return SourceRange(start=Position(line=2, character=0), end=Position(line=2, character=3))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("# Title\n\n**bold** and *italic* with `int *p`", "Title\n\nbold and italic with int *p"),
        ("```cpp\nint **ptr; // **literal**\n```", "int **ptr; // **literal**"),
        ("[docs](https://example.com)", "docs (https://example.com)"),
        ("<https://example.com>", "https://example.com"),
        (r"\*literal\*", "*literal*"),
    ],
)
def test_hover_markdown_becomes_plain_text_without_damaging_code(source, expected):
    """Remove formatting through Markdown tokens while preserving literals and links."""
    assert markdown_text(source) == expected


def test_hover_keeps_plain_and_code_fragments_verbatim():
    """Convert only Markdown fragments and retain unknown or empty hover outcomes."""
    hover = Hover(
        contents=[
            HoverText(kind="markdown", text="**add**"),
            HoverText(kind="code", text="int **ptr;", language="cpp"),
            HoverText(kind="plaintext", text="**literal**"),
        ],
        range=sample_range(),
    )
    assert hover_text(hover).startswith("add\n\nint **ptr;\n\n**literal**")
    assert "line 2, character 0" in hover_text(hover)
    assert hover_text(None) == "No hover information available."


def test_compiler_style_diagnostics_keep_related_notes_and_readable_tags():
    """Present warnings with origin and notes, without replacing unknown values with labels."""
    path = WorkspacePath("project/α.cpp")
    diagnostic = Diagnostic(
        range=sample_range(),
        severity="warning",
        source="clang",
        code="unused",
        message="unused value\nremove it",
        tags=[1, 2, 99],
        related=[
            RelatedDiagnostic(
                location=NavigationLocation(path=path, range=sample_range()),
                message="declared here",
            ),
        ],
    )
    text = diagnostic_text(path, [diagnostic])
    assert "warning (clang, unused): unused value\n    remove it" in text
    assert "Tags: unnecessary, deprecated, unknown tag (99)" in text
    assert "Note at project/α.cpp, line 2, character 0" in text
    assert "declared here" in text
    answer = DiagnosticsResult(configurations=["Debug", "Release"], path=path, diagnostics=[])
    empty = render_answers([answer], "Diagnostics")
    assert "Configurations: Debug, Release" in empty
    assert "No diagnostics reported." in empty


def test_external_location_has_no_invented_preview():
    """Show external coordinates without performing a filesystem read."""
    location = NavigationLocation(
        path=WorkspacePath("root/C:/SDK/header.hpp"),
        range=sample_range(),
    )
    assert location_text(location).endswith("Source preview unavailable.")


def test_symbol_tree_keeps_children_unknown_kinds_and_distinct_selection_ranges():
    """Preserve hierarchical structure, detail text, tags, and both source ranges."""
    child = DocumentSymbol(
        name="nested",
        kind=99,
        range=sample_range(),
        selection_range=SourceRange(
            start=Position(line=2, character=1),
            end=Position(line=2, character=2),
        ),
        detail="int nested",
        tags=[1],
    )
    parent = DocumentSymbol(
        name="scope",
        kind=3,
        range=sample_range(),
        selection_range=sample_range(),
        children=[child],
    )
    text = document_symbols_text([parent])
    assert "scope (namespace)" in text and "nested (unknown kind (99))" in text
    assert "int nested" in text and "Tags: deprecated" in text
    assert "Selection: line 2, character 1" in text
    assert "└──" in text
