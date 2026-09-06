from pathlib import Path

import pytest
from mcp import Client
from mcp.client import advertise
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.types import PromptReference, ResourceTemplateReference

from forgemcp.server import create_server
from forgemcp.workspace.service import WorkspaceService


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def isolate_host_discovery(monkeypatch):
    """Workspace protocol tests must not depend on host installations."""
    from forgemcp.toolchain import discovery
    from forgemcp.toolchain.providers import visual_studio
    monkeypatch.setattr(discovery, "load_tools", lambda: ())
    async def no_visual_studio(*args):
        return ()
    monkeypatch.setattr(visual_studio, "discover", no_visual_studio)


@pytest.mark.anyio
async def test_server_exposes_app_tool_progress_and_structured_output(
    cpp_acceptance_project: Path,
) -> None:
    server = create_server(cpp_acceptance_project)
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
        app = await client.read_resource(WorkspaceService.WIDGET.uri)

        assert WorkspaceService.__doc__ in client.instructions

    assert tool.meta is not None
    assert tool.meta["ui"]["resourceUri"] == WorkspaceService.WIDGET.uri
    assert tool.description == "Summarize the configured C/C++ workspace without modifying it."
    assert tool.output_schema is not None
    assert tool.annotations is not None
    assert tool.annotations.title is None
    assert tool.icons
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["source_files"] == 12
    assert [item[0] for item in progress] == [1.0, 2.0, 3.0]
    assert app.contents[0].mime_type == APP_MIME_TYPE


@pytest.mark.anyio
async def test_server_exposes_resource_prompt_and_completions(
    cpp_acceptance_project: Path,
) -> None:
    server = create_server(cpp_acceptance_project)

    async with Client(server, raise_exceptions=True) as client:
        templates = await client.list_resource_templates()
        prompts = await client.list_prompts()
        resource_completion = await client.complete(
            ResourceTemplateReference(uri=WorkspaceService.FILES_URI),
            {"name": "extension", "value": "cp"},
        )
        prompt_completion = await client.complete(
            PromptReference(name=WorkspaceService.PROMPT),
            {"name": "focus", "value": "to"},
        )
        resource = await client.read_resource("forgemcp://workspace/files/cpp")

    assert any(
        item.uri_template == WorkspaceService.FILES_URI for item in templates.resource_templates
    )
    assert any(item.name == WorkspaceService.PROMPT for item in prompts.prompts)
    assert resource_completion.completion.values == ["cpp"]
    assert prompt_completion.completion.values == ["toolchain"]
    assert "src/math.cpp" in resource.contents[0].text


@pytest.mark.anyio
async def test_default_workspace_is_server_process_directory(
    cpp_acceptance_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(cpp_acceptance_project)

    async with Client(create_server(), raise_exceptions=True) as client:
        resource = await client.read_resource("forgemcp://workspace/files/cpp")

    assert "src/math.cpp" in resource.contents[0].text
