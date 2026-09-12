"""Small helpers for building Markdown documents."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Self

_SAFE_START = "\ue000"
_SAFE_END = "\ue001"


def _escape(text: str) -> str:
    """Escape characters that can introduce Markdown markup."""
    text = text.replace("\\", "\\\\")
    for character in "`*_[]<>#|":
        text = text.replace(character, f"\\{character}")
    return text


def _render_text(text: str) -> str:
    """Escape plain text while preserving nodes embedded through an f-string."""
    parts: list[str] = []
    position = 0

    while (start := text.find(_SAFE_START, position)) != -1:
        end = text.find(_SAFE_END, start + 1)
        if end == -1:
            break
        parts.append(_escape(text[position:start]))
        parts.append(text[start + 1 : end])
        position = end + 1

    parts.append(_escape(text[position:]))
    return "".join(parts)


def _render_content(content: Content) -> str:
    if isinstance(content, Node):
        return content.render()
    return _render_text(content)


class Node(ABC):
    """Base class for every Markdown element."""

    @abstractmethod
    def render(self) -> str:
        """Render this node as Markdown."""

    def __str__(self) -> str:
        return self.render()

    def __format__(self, format_spec: str) -> str:
        rendered = format(self.render(), format_spec)
        return f"{_SAFE_START}{rendered}{_SAFE_END}"


Content = str | Node


class Text(Node):
    """Escaped plain text."""

    def __init__(self, text: str) -> None:
        self.text = text

    def render(self) -> str:
        return _render_text(self.text)


class Paragraph(Node):
    """A paragraph."""

    def __init__(self, content: Content) -> None:
        self.content = content

    def render(self) -> str:
        return _render_content(self.content)


class Bold(Node):
    """Bold text."""

    def __init__(self, content: Content) -> None:
        self.content = content

    def render(self) -> str:
        return f"**{_render_content(self.content)}**"


class Italic(Node):
    """Italic text."""

    def __init__(self, content: Content) -> None:
        self.content = content

    def render(self) -> str:
        return f"*{_render_content(self.content)}*"


class Strikethrough(Node):
    """Strikethrough text."""

    def __init__(self, content: Content) -> None:
        self.content = content

    def render(self) -> str:
        return f"~~{_render_content(self.content)}~~"


class Heading(Node):
    """A heading from level one through six."""

    def __init__(self, content: Content, level: int = 1) -> None:
        if level not in range(1, 7):
            raise ValueError("heading level must be between 1 and 6")
        self.content = content
        self.level = level

    def render(self) -> str:
        return f"{'#' * self.level} {_render_content(self.content)}"


class Link(Node):
    """A link."""

    def __init__(self, text: Content, url: str) -> None:
        self.text = text
        self.url = url

    def render(self) -> str:
        url = self.url.replace("\\", "\\\\").replace(")", "\\)")
        return f"[{_render_content(self.text)}]({url})"


class Image(Node):
    """An image."""

    def __init__(self, alt: Content, url: str) -> None:
        self.alt = alt
        self.url = url

    def render(self) -> str:
        url = self.url.replace("\\", "\\\\").replace(")", "\\)")
        return f"![{_render_content(self.alt)}]({url})"


class InlineCode(Node):
    """Inline code."""

    def __init__(self, code: str) -> None:
        self.code = code

    def render(self) -> str:
        return f"`{self.code}`"


class CodeBlock(Node):
    """A fenced code block."""

    def __init__(self, code: str, language: str = "") -> None:
        self.code = code
        self.language = language

    def render(self) -> str:
        return f"```{self.language}\n{self.code}\n```"


class Blockquote(Node):
    """A block quote."""

    def __init__(self, content: Content) -> None:
        self.content = content

    def render(self) -> str:
        return "\n".join(f"> {line}" for line in _render_content(self.content).splitlines())


class HorizontalRule(Node):
    """A horizontal rule."""

    def render(self) -> str:
        return "---"


def _render_list_item(prefix: str, item: Content) -> str:
    lines = _render_content(item).splitlines() or [""]
    indentation = " " * len(prefix)
    return "\n".join([f"{prefix}{lines[0]}", *(f"{indentation}{line}" for line in lines[1:])])


class UnorderedList(Node):
    """An unordered list."""

    def __init__(self, items: Iterable[Content] = ()) -> None:
        self.items = list(items)

    def add(self, item: Content) -> Self:
        self.items.append(item)
        return self

    def render(self) -> str:
        return "\n".join(_render_list_item("- ", item) for item in self.items)


class OrderedList(Node):
    """An ordered list."""

    def __init__(self, items: Iterable[Content] = ()) -> None:
        self.items = list(items)

    def add(self, item: Content) -> Self:
        self.items.append(item)
        return self

    def render(self) -> str:
        return "\n".join(
            _render_list_item(f"{index}. ", item)
            for index, item in enumerate(self.items, start=1)
        )


class Table(Node):
    """A table with a header and rows."""

    def __init__(
        self,
        headers: Sequence[Content],
        rows: Iterable[Sequence[Content]] = (),
    ) -> None:
        if not headers:
            raise ValueError("table must have at least one column")
        self.headers = list(headers)
        self.rows: list[list[Content]] = []
        for row in rows:
            self.add(row)

    def add(self, row: Sequence[Content]) -> Self:
        if len(row) != len(self.headers):
            raise ValueError("table row must have the same number of cells as the header")
        self.rows.append(list(row))
        return self

    def render(self) -> str:
        def render_row(row: Sequence[Content]) -> str:
            cells = (_render_content(cell).replace("\n", " ") for cell in row)
            return f"| {' | '.join(cells)} |"

        separator = f"| {' | '.join('---' for _ in self.headers)} |"
        return "\n".join(
            [render_row(self.headers), separator, *(render_row(row) for row in self.rows)]
        )


class Document(Node):
    """A complete Markdown document."""

    def __init__(self, nodes: Iterable[Node] = ()) -> None:
        self.nodes = list(nodes)

    def add(self, node: Node) -> None:
        self.nodes.append(node)

    def render(self) -> str:
        if not self.nodes:
            return ""
        rendered = "\n\n".join(node.render() for node in self.nodes)
        return f"{rendered}\n"


__all__ = [
    "Blockquote",
    "Bold",
    "CodeBlock",
    "Document",
    "Heading",
    "HorizontalRule",
    "Image",
    "InlineCode",
    "Italic",
    "Link",
    "Node",
    "OrderedList",
    "Paragraph",
    "Strikethrough",
    "Table",
    "Text",
    "UnorderedList",
]
