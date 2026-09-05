from pathlib import Path

import pytest
from mcp import Client
from mcp.client import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.types import PromptReference, ResourceTemplateReference

from forgemcp.server import create_server
from forgemcp.workspace.service import (
    INSPECT_WORKSPACE_PROMPT,
    WORKSPACE_APP_URI,
    WORKSPACE_FILES_URI,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_server_exposes_app_tool_progress_and_structured_output(tmp_path: Path) -> None:
    (tmp_path / "main.cpp").write_text("int main() {}", encoding="utf-8")
    server = create_server(tmp_path)
    progress: list[tuple[float, float | None, str | None]] = []

    async def collect(value: float, total: float | None, message: str | None) -> None:
        progress.append((value, total, message))

    async with Client(
        server,
        extensions=[advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})],
        raise_exceptions=True,
    ) as client:
        tools = await client.list_tools()
        tool = next(item for item in tools.tools if item.name == "workspace_overview")
        result = await client.call_tool("workspace_overview", {}, progress_callback=collect)
        app = await client.read_resource(WORKSPACE_APP_URI)

    assert tool.meta is not None
    assert tool.meta["ui"]["resourceUri"] == WORKSPACE_APP_URI
    assert tool.icons
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["source_files"] == 1
    assert [item[0] for item in progress] == [1.0, 2.0, 3.0]
    assert app.contents[0].mime_type == APP_MIME_TYPE


@pytest.mark.anyio
async def test_server_exposes_resource_prompt_and_completions(tmp_path: Path) -> None:
    (tmp_path / "main.cpp").write_text("", encoding="utf-8")
    server = create_server(tmp_path)

    async with Client(server, raise_exceptions=True) as client:
        templates = await client.list_resource_templates()
        prompts = await client.list_prompts()
        resource_completion = await client.complete(
            ResourceTemplateReference(uri=WORKSPACE_FILES_URI),
            {"name": "extension", "value": "cp"},
        )
        prompt_completion = await client.complete(
            PromptReference(name=INSPECT_WORKSPACE_PROMPT),
            {"name": "focus", "value": "to"},
        )
        resource = await client.read_resource("forgemcp://workspace/files/cpp")

    assert any(item.uri_template == WORKSPACE_FILES_URI for item in templates.resource_templates)
    assert any(item.name == INSPECT_WORKSPACE_PROMPT for item in prompts.prompts)
    assert resource_completion.completion.values == ["cpp"]
    assert prompt_completion.completion.values == ["toolchain"]
    assert "main.cpp" in resource.contents[0].text
