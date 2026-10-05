"""Shared response serialization and plain source-line presentation."""

from datetime import datetime, timezone
from typing import Annotated

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from forgemcp.text import numbered_lines, timestamp, tool_result
from forgemcp.workspace.path import WorkspacePath


class SampleResult(BaseModel):
    """Exercise aliased fields, qualified paths, Unicode, and exact timestamps."""

    path: WorkspacePath
    created: datetime
    label: str = Field(alias="name")


@pytest.fixture
def anyio_backend():
    """Use the asyncio backend required by the MCP in-process client."""
    return "asyncio"


@pytest.mark.anyio
async def test_explicit_result_preserves_schema_aliases_and_list_envelopes():
    """Validate explicit responses through the SDK without replacing their readable text."""
    server = MCPServer("typed-text")
    data = SampleResult(
        path=WorkspacePath("project/α.cpp"),
        created=datetime(2026, 10, 5, 12, 0, 0, 123456, tzinfo=timezone.utc),
        name="α",
    )

    @server.tool()
    def single() -> Annotated[CallToolResult, SampleResult]:
        """Return an object with independently chosen text."""
        return tool_result(data, "Readable object.")

    @server.tool()
    def multiple() -> Annotated[CallToolResult, list[SampleResult]]:
        """Return a collection using the SDK's existing result envelope."""
        return tool_result([data], "Readable collection.")

    async with Client(server) as client:
        schemas = {tool.name: tool.output_schema for tool in (await client.list_tools()).tools}
        assert "name" in schemas["single"]["properties"]
        assert schemas["multiple"]["properties"]["result"]["type"] == "array"
        item = await client.call_tool("single", {})
        collection = await client.call_tool("multiple", {})
        assert not item.is_error and not collection.is_error
        assert item.content[0].text == "Readable object."
        assert collection.content[0].text == "Readable collection."
        assert item.structured_content == data.model_dump(mode="json", by_alias=True)
        assert collection.structured_content == {"result": [item.structured_content]}


@pytest.mark.parametrize(
    ("text", "start", "expected"),
    [
        ("", 1, ""),
        ("α\r\n\r\nβ\r\n", 9, " 9 | α\n10 | \n11 | β"),
        ("```\n**literal**", 2, "2 | ```\n3 | **literal**"),
        ("one\n", 1, "1 | one"),
    ],
)
def test_numbered_source_lines_preserve_content(text, start, expected):
    """Keep empty and literal source lines without inventing a trailing phantom line."""
    assert numbered_lines(text, start) == expected


def test_timestamp_preserves_precision_and_handles_unavailable_metadata():
    """Render exact UTC instants without assigning a value to missing metadata."""
    instant = datetime(2026, 10, 5, 12, 0, 0, 123456, tzinfo=timezone.utc)
    assert timestamp(instant) == "2026-10-05T12:00:00.123456Z"
    assert timestamp(None) == "Unavailable"
