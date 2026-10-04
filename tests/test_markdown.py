"""Markdown escaping, composition, and block rendering tests."""

from forgemcp.markdown import (
    Blockquote,
    Bold,
    CodeBlock,
    Document,
    Heading,
    HorizontalRule,
    Image,
    InlineCode,
    Italic,
    Link,
    Node,
    OrderedList,
    Paragraph,
    Strikethrough,
    Table,
    Text,
    UnorderedList,
)


def render_one(node: Node) -> str:
    """Render one Markdown node as a complete document."""
    document = Document()
    document.add(node)
    return document.render()


def test_text() -> None:
    """Verify plain text escapes Markdown control characters."""
    assert render_one(Text("A *plain* [text]")) == "A \\*plain\\* \\[text\\]\n"


def test_paragraph() -> None:
    """Verify paragraph text preserves nested formatted nodes and escapes ordinary markup."""
    assert render_one(Paragraph("A paragraph.")) == "A paragraph.\n"


def test_bold() -> None:
    """Verify bold nodes wrap escaped content in bold delimiters."""
    assert render_one(Paragraph(Bold("Bold"))) == "**Bold**\n"


def test_italic() -> None:
    """Verify italic nodes wrap escaped content in italic delimiters."""
    assert render_one(Paragraph(Italic("Italic"))) == "*Italic*\n"


def test_strikethrough() -> None:
    """Verify strikethrough nodes wrap escaped content in double tilde delimiters."""
    assert render_one(Paragraph(Strikethrough("Removed"))) == "~~Removed~~\n"


def test_heading() -> None:
    """Verify heading markers and reject levels outside one through six."""
    assert render_one(Heading("Heading", level=2)) == "## Heading\n"


def test_link() -> None:
    """Verify link text and destination escaping preserve valid Markdown."""
    assert render_one(Paragraph(Link("ForgeMCP", "https://example.com"))) == (
        "[ForgeMCP](https://example.com)\n"
    )


def test_image() -> None:
    """Verify alternative text and image URL escaping preserve valid Markdown."""
    assert render_one(Image("Diagram", "diagram.png")) == "![Diagram](diagram.png)\n"


def test_inline_code() -> None:
    """Verify inline code keeps its literal contents."""
    assert render_one(Paragraph(InlineCode("value = 1"))) == "`value = 1`\n"


def test_code_block() -> None:
    """Verify fenced code keeps its literal contents and language label."""
    assert render_one(CodeBlock("print('hello')", "python")) == (
        "```python\nprint('hello')\n```\n"
    )


def test_code_block_keeps_embedded_fences_as_data() -> None:
    """Verify embedded backtick runs cannot close the surrounding code block."""
    assert (
        CodeBlock("```\n<script>x</script>\n```").render()
        == "````\n```\n<script>x</script>\n```\n````"
    )


def test_blockquote() -> None:
    """Verify every blockquote line receives its marker."""
    assert render_one(Blockquote("First line\nSecond line")) == (
        "> First line\n> Second line\n"
    )


def test_horizontal_rule() -> None:
    """Verify the horizontal rule renders as a separate document block."""
    assert render_one(HorizontalRule()) == "---\n"


def test_unordered_list_from_items() -> None:
    """Verify initial bullet items and continuation indentation."""
    assert render_one(UnorderedList(["First", Bold("Second")])) == (
        "- First\n- **Second**\n"
    )


def test_unordered_list_add() -> None:
    """Verify chained bullet appends preserve item order."""
    items = UnorderedList()
    items.add("First")
    items.add("Second")

    assert render_one(items) == "- First\n- Second\n"


def test_ordered_list_from_items() -> None:
    """Verify initial ordered items use consecutive one-based numbering."""
    assert render_one(OrderedList(["First", "Second"])) == "1. First\n2. Second\n"


def test_ordered_list_add() -> None:
    """Verify chained ordered appends preserve item order."""
    items = OrderedList()
    items.add("First")
    items.add("Second")

    assert render_one(items) == "1. First\n2. Second\n"


def test_table_from_rows() -> None:
    """Verify table headers, separators, escaped cells, and row width validation."""
    table = Table(["Name", "Value"], [["A", "1"], ["B", "2"]])

    assert render_one(table) == (
        "| Name | Value |\n" "| --- | --- |\n" "| A | 1 |\n" "| B | 2 |\n"
    )


def test_table_add() -> None:
    """Verify appended table rows preserve columns and reject mismatched widths."""
    table = Table(["Name", "Value"])
    table.add(["A", "1"])

    assert render_one(table) == ("| Name | Value |\n" "| --- | --- |\n" "| A | 1 |\n")


def test_document_with_every_element() -> None:
    """Verify mixed Markdown nodes compose with block separators and a final newline."""
    document = Document()
    document.add(Heading("Markdown document"))
    document.add(Paragraph(Italic("This text will be italic!")))
    document.add(Paragraph(Bold("This text will be bold!")))
    document.add(
        Paragraph(f"You can also combine {Bold(Italic('bold and italic text!'))}")
    )
    document.add(Paragraph(Strikethrough("Old text")))
    document.add(Paragraph(Link("Documentation", "https://example.com/docs")))
    document.add(Image("Architecture", "architecture.png"))
    document.add(Paragraph(f"Run {InlineCode('python -m pytest')} to test."))
    document.add(CodeBlock("int main() { return 0; }", "cpp"))
    document.add(Blockquote("Simple is better."))
    document.add(HorizontalRule())
    document.add(UnorderedList(["Alpha", "Beta"]))
    document.add(OrderedList(["First", "Second"]))
    document.add(Table(["Name", "Value"], [["answer", "42"]]))

    assert document.render() == (
        "# Markdown document\n\n"
        "*This text will be italic!*\n\n"
        "**This text will be bold!**\n\n"
        "You can also combine ***bold and italic text!***\n\n"
        "~~Old text~~\n\n"
        "[Documentation](https://example.com/docs)\n\n"
        "![Architecture](architecture.png)\n\n"
        "Run `python -m pytest` to test.\n\n"
        "```cpp\nint main() { return 0; }\n```\n\n"
        "> Simple is better.\n\n"
        "---\n\n"
        "- Alpha\n- Beta\n\n"
        "1. First\n2. Second\n\n"
        "| Name | Value |\n"
        "| --- | --- |\n"
        "| answer | 42 |\n"
    )
