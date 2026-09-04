"""Focused MCP App bindings and static-asset gates for Core and Workspace."""

from __future__ import annotations

import asyncio
from importlib.resources import files
from pathlib import Path

from forgemcp.core.application import ForgeApplication
from forgemcp.core.config import ForgeConfig
from forgemcp.plugins import MCP_APP_HTML_MIME_TYPE
from forgemcp.server import SERVER_STATUS_APP_URI, create_server, server_status
from forgemcp.workspace.plugin import WORKSPACE_RESULT_APP_URI


_ROOT = Path(__file__).parents[1]
_CSP = {
    "connectDomains": [],
    "resourceDomains": [],
    "frameDomains": [],
    "baseUriDomains": [],
}
_WORKSPACE_TOOLS = (
    "workspace__list_files",
    "workspace__read_text",
    "workspace__get_snapshot",
    "workspace__apply_unified_patch",
    "workspace__apply_text_edits",
)


def test_workspace_app_registers_one_resource_for_all_public_workspace_tools(tmp_path: Path) -> None:
    async def exercise() -> None:
        (tmp_path / "note.txt").write_bytes(b"before\n")
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        await application.start()
        try:
            plugins = application.services.get("plugins")
            resource = next(item for item in plugins.apps.resources() if item.uri == WORKSPACE_RESULT_APP_URI)
            assert resource.name == "forgemcp_workspace_result_app"
            assert resource.resource_meta() == {"ui": {"csp": _CSP, "prefersBorder": True}}
            assert all(
                plugins.apps.binding_for(tool_name).resource_uri == WORKSPACE_RESULT_APP_URI
                and plugins.apps.binding_for(tool_name).visibility == ("model", "app")
                for tool_name in _WORKSPACE_TOOLS
            )
            tools = {tool.name: tool for tool in plugins.tools.contributions()}
            result = await tools["workspace__read_text"].handler({"path": "note.txt"})
            assert result["path"] == "note.txt"
            assert result["text"] == "before\n"
            assert result["snapshot"]["sha256"] is not None
        finally:
            await application.aclose()

    asyncio.run(exercise())


def test_server_status_app_is_registered_during_server_lifespan_without_changing_status_result(tmp_path: Path) -> None:
    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        server = create_server(lambda: application)
        async with server._mcp_server.lifespan(server._mcp_server):  # type: ignore[attr-defined]
            status = server_status(application)
            assert status == {
                "version": "0.1.0",
                "workspace_root": "configured",
                "state": "running",
                "services": [
                    "config", "logger", "plugins", "process_runtime", "project_status_registry",
                    "project_status_service", "toolchain_discovery", "workspace",
                ],
            }
            plugins = application.services.get("plugins")
            resource = next(item for item in plugins.apps.resources() if item.uri == SERVER_STATUS_APP_URI)
            assert resource.name == "forgemcp_server_status_app"
            assert resource.resource_meta() == {"ui": {"csp": _CSP, "prefersBorder": True}}
            tools = {item.name: item for item in await server.list_tools()}
            tool = tools["server_status"]
            assert tool.meta == {"ui": {"resourceUri": SERVER_STATUS_APP_URI, "visibility": ["model", "app"]}}
            resources = await server.list_resources()
            server_resource = next(item for item in resources if str(item.uri) == SERVER_STATUS_APP_URI)
            assert server_resource.mimeType == MCP_APP_HTML_MIME_TYPE
            assert server_resource.meta == resource.resource_meta()
            workspace_resource = next(item for item in resources if str(item.uri) == WORKSPACE_RESULT_APP_URI)
            assert workspace_resource.mimeType == MCP_APP_HTML_MIME_TYPE
            assert workspace_resource.meta == {"ui": {"csp": _CSP, "prefersBorder": True}}
            for tool_name in _WORKSPACE_TOOLS:
                assert tools[tool_name].meta == {
                    "ui": {"resourceUri": WORKSPACE_RESULT_APP_URI, "visibility": ["model", "app"]}
                }

    asyncio.run(exercise())


def test_core_workspace_assets_are_packaged_static_and_use_safe_dom_source() -> None:
    for asset_name, source_name, app_name in (
        ("server-status.html", "server-status-app.js", "forgemcp-server-status"),
        ("workspace-result.html", "workspace-result-app.js", "forgemcp-workspace-result"),
    ):
        html = files("forgemcp.apps.assets").joinpath(asset_name).read_text(encoding="utf-8")
        source = (_ROOT / "frontend" / "core-workspace-apps" / source_name).read_text(encoding="utf-8")
        assert html.startswith("<!doctype html>")
        assert "source-sha256:" in html
        assert app_name in html
        assert "textContent" in source
        for forbidden in (
            "callServerTool", "tools/call", "resources/read", "fetch(", "XMLHttpRequest", "WebSocket",
            "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function(",
            "localStorage", "sessionStorage", "ui/open-link", "ui/update-model-context", "requestDisplayMode",
        ):
            assert forbidden not in source
    workspace_source = (_ROOT / "frontend" / "core-workspace-apps" / "workspace-result-app.js").read_text(encoding="utf-8")
    assert "MAX_FILES = 1000" in workspace_source
    assert "MAX_RENDER_TEXT" in workspace_source
    assert "safeCodeLine" in workspace_source
