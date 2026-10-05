"""Plain-text language answers with source coordinates and readable LSP labels."""

from collections.abc import Sequence

from markdown_it import MarkdownIt
from markdown_it.token import Token

from forgemcp.cmake.models import CompilationContext
from forgemcp.text import numbered_lines
from forgemcp.workspace.path import WorkspacePath

from .models import (
    Diagnostic,
    DocumentSymbol,
    Hover,
    NavigationLocation,
    SourceRange,
    DiagnosticsResult,
    HoverResult,
    DefinitionResult,
    ReferencesResult,
    DocumentSymbolsResult,
    WorkspaceSymbolsResult,
)


def source_range(span: SourceRange) -> str:
    """Label one-based lines and zero-based characters without changing their coordinates."""
    start = span.start
    end = span.end
    return (
        f"line {start.line}, character {start.character} to "
        f"line {end.line}, character {end.character}"
    )


def symbol_kind(kind: int) -> str:
    """Translate known LSP symbol kinds and preserve unknown numeric values."""
    names = (
        "file",
        "module",
        "namespace",
        "package",
        "class",
        "method",
        "property",
        "field",
        "constructor",
        "enum",
        "interface",
        "function",
        "variable",
        "constant",
        "string",
        "number",
        "boolean",
        "array",
        "object",
        "key",
        "null",
        "enum member",
        "struct",
        "event",
        "operator",
        "type parameter",
    )
    return names[kind - 1] if 1 <= kind <= len(names) else f"unknown kind ({kind})"


def tags(values: list[int], *, diagnostic: bool = False) -> str:
    """Explain known tags and retain unknown values without inventing their meaning."""
    names = {1: "unnecessary", 2: "deprecated"} if diagnostic else {1: "deprecated"}
    return ", ".join(names.get(value, f"unknown tag ({value})") for value in values)


def inline_text(tokens: Sequence[Token]) -> str:
    """Remove inline formatting while retaining code, line breaks, and link destinations."""
    parts: list[str] = []
    links: list[tuple[str, int]] = []
    for token in tokens:
        if token.type == "link_open":
            links.append((token.attrGet("href") or "", len(parts)))
        elif token.type == "link_close" and links:
            target, start = links.pop()
            if target and "".join(parts[start:]) != target:
                parts.append(f" ({target})")
        elif token.type in ("softbreak", "hardbreak"):
            parts.append("\n")
        elif token.type == "image":
            parts.append(f"Image: {token.content} ({token.attrGet('src') or ''})")
        elif token.type in ("text", "code_inline", "html_inline"):
            parts.append(token.content)
    return "".join(parts)


def markdown_text(text: str) -> str:
    """Convert clangd Markdown fragments to text while leaving code contents untouched."""
    blocks: list[str] = []
    list_depth = 0
    for token in MarkdownIt("commonmark", {"html": False}).parse(text):
        if token.type in ("bullet_list_open", "ordered_list_open"):
            list_depth += 1
        elif token.type in ("bullet_list_close", "ordered_list_close"):
            list_depth -= 1
        elif token.type == "inline":
            value = inline_text(token.children or [])
            blocks.append(("  " * list_depth if list_depth else "") + value)
        elif token.type in ("fence", "code_block"):
            blocks.append(token.content.rstrip("\n"))
        elif token.type == "hr":
            blocks.append("────────────────────")
    return "\n\n".join(blocks)


def hover_text(hover: Hover | None) -> str:
    """Show every hover fragment as text and identify the associated range when supplied."""
    if hover is None:
        return "No hover information available."
    blocks = [
        markdown_text(fragment.text) if fragment.kind == "markdown" else fragment.text
        for fragment in hover.contents
    ]
    if not blocks:
        blocks.append("No hover content reported.")
    if hover.range is not None:
        blocks.append(f"Range: {source_range(hover.range)}")
    return "\n\n".join(blocks)


def diagnostic_text(path: WorkspacePath, diagnostics: list[Diagnostic]) -> str:
    """Format compiler-style messages and related notes without dry numeric summaries."""
    if not diagnostics:
        return "No diagnostics reported."
    blocks = []
    for diagnostic in diagnostics:
        origin = ", ".join(
            str(value)
            for value in (diagnostic.source, diagnostic.code)
            if value is not None
        )
        label = diagnostic.severity or "severity unavailable"
        if origin:
            label += f" ({origin})"
        lines = [
            f"{path}, {source_range(diagnostic.range)}",
            f"  {label}: " + diagnostic.message.replace("\n", "\n    "),
        ]
        if diagnostic.tags:
            lines.append(f"  Tags: {tags(diagnostic.tags, diagnostic=True)}")
        for note in diagnostic.related:
            lines.extend(
                [
                    f"  Note at {note.location.path}, {source_range(note.location.range)}:",
                    "    " + note.message.replace("\n", "\n    "),
                ]
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def location_text(location: NavigationLocation) -> str:
    """Show saved source context without reading an external or managed file again."""
    lines = [f"{location.path}, {source_range(location.range)}"]
    if location.preview is not None:
        lines.append(numbered_lines(location.preview.text, location.preview.start_line))
    else:
        lines.append("  Source preview unavailable.")
    return "\n".join(lines)


def document_symbols_text(symbols: list[DocumentSymbol]) -> str:
    """Draw the full symbol hierarchy with signatures, ranges, and readable kinds."""
    lines: list[str] = []

    def visit(children: list[DocumentSymbol], prefix: str = "") -> None:
        """Append one level and recursively preserve every child symbol."""
        for index, symbol in enumerate(children):
            last = index == len(children) - 1
            label = f"{symbol.name} ({symbol_kind(symbol.kind)})"
            lines.append(f"{prefix}{'└── ' if last else '├── '}{label}")
            continuation = prefix + ("    " if last else "│   ")
            if symbol.detail is not None:
                lines.append(continuation + symbol.detail.replace("\n", "\n" + continuation))
            lines.append(f"{continuation}Range: {source_range(symbol.range)}")
            lines.append(f"{continuation}Selection: {source_range(symbol.selection_range)}")
            if symbol.tags:
                lines.append(f"{continuation}Tags: {tags(symbol.tags)}")
            visit(symbol.children, continuation)

    visit(symbols)
    return "\n".join(lines) if lines else "No document symbols found."


def render_configurations(contexts: list[CompilationContext]) -> str:
    """Identify all available compilation contexts and their toolset and database paths."""
    lines = [f"Available clangd configurations: {len(contexts)}."]
    for context in contexts:
        lines.extend(
            [
                "",
                context.id,
                f"  Toolset: {context.toolset_id}",
                f"  Build directory: {context.build_directory}",
                f"  Compilation database: {context.compilation_database}",
            ]
        )
    return "\n".join(lines)


def render_answers(
    answers: list[DiagnosticsResult] | list[HoverResult] | list[DefinitionResult]
    | list[ReferencesResult] | list[DocumentSymbolsResult] | list[WorkspaceSymbolsResult],
    title: str,
) -> str:
    """Keep complete answers grouped by their configuration provenance."""
    blocks = [title, "Coordinates: lines are one-based; characters are zero-based."]
    for answer in answers:
        blocks.append("Configurations: " + ", ".join(answer.configurations))
        if isinstance(answer, DiagnosticsResult):
            blocks.append(diagnostic_text(answer.path, answer.diagnostics))
        elif isinstance(answer, HoverResult):
            blocks.append(hover_text(answer.hover))
        elif isinstance(answer, (DefinitionResult, ReferencesResult)):
            blocks.append(
                "\n\n".join(location_text(location) for location in answer.locations)
                if answer.locations
                else "No locations found."
            )
        elif isinstance(answer, DocumentSymbolsResult):
            blocks.append(document_symbols_text(answer.symbols))
        else:
            if not answer.symbols:
                blocks.append("No workspace symbols found.")
            for symbol in answer.symbols:
                lines = [f"{symbol.name} ({symbol_kind(symbol.kind)})"]
                if symbol.container_name is not None:
                    lines.append(f"  Scope: {symbol.container_name}")
                if symbol.tags:
                    lines.append(f"  Tags: {tags(symbol.tags)}")
                lines.append(location_text(symbol.location))
                blocks.append("\n".join(lines))
    if not answers:
        blocks.append("No configuration answers returned.")
    return "\n\n".join(blocks)
