"""Plain-text source excerpts and explicit typed MCP responses."""

from collections.abc import Sequence
from datetime import datetime, timezone

from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel


def tool_result(data: BaseModel | Sequence[BaseModel], text: str) -> CallToolResult:
    """Keep model fields and SDK list envelopes beside a custom text fallback."""
    structured = (
        data.model_dump(mode="json", by_alias=True)
        if isinstance(data, BaseModel)
        else {"result": [item.model_dump(mode="json", by_alias=True) for item in data]}
    )
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=structured,
    )


def numbered_lines(text: str, start: int = 1) -> str:
    """Prefix source lines with their absolute numbers without Markdown fencing."""
    lines = text.splitlines()
    width = len(str(start + len(lines) - 1))
    return "\n".join(
        f"{number:>{width}} | {line}"
        for number, line in enumerate(lines, start=start)
    )


def timestamp(value: datetime | None) -> str:
    """Show an exact UTC timestamp or explicitly unavailable metadata."""
    if value is None:
        return "Unavailable"
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
