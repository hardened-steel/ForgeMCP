"""Focused registration, packaging, and safety checks for Quality MCP Apps."""

from __future__ import annotations

import asyncio
from importlib.resources import files
import os
from pathlib import Path
import sys

from mcp import ClientSession, types as mcp_types
from mcp.client.stdio import StdioServerParameters, stdio_client

from forgemcp import __version__
from forgemcp.core.application import ForgeApplication
from forgemcp.core.config import ForgeConfig
from forgemcp.plugins import MCP_APP_HTML_MIME_TYPE, PluginManager
from forgemcp.quality.plugin import QUALITY_FINDINGS_APP_URI, QUALITY_OVERVIEW_APP_URI


_ROOT = Path(__file__).parents[1]
_OVERVIEW_TOOLS = frozenset((
    "quality__status",
    "clang_format__check",
    "clang_format__apply",
    "clang_tidy__list_checks",
))
_FINDINGS_TOOLS = frozenset(("clang_tidy__run", "sanitizer__parse_report"))
_APPS_CAPABILITY = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": [MCP_APP_HTML_MIME_TYPE]}}}


class _AppsClientSession(ClientSession):
    """Official SDK session with the stable Apps extension capability added."""

    async def initialize(self) -> mcp_types.InitializeResult:
        result = await self.send_request(
            mcp_types.ClientRequest(
                mcp_types.InitializeRequest(
                    params=mcp_types.InitializeRequestParams(
                        protocolVersion=mcp_types.LATEST_PROTOCOL_VERSION,
                        capabilities=mcp_types.ClientCapabilities.model_validate(_APPS_CAPABILITY),
                        clientInfo=mcp_types.Implementation(name="forgemcp-quality-apps-test", version="1.0.0"),
                    )
                )
            ),
            mcp_types.InitializeResult,
        )
        self._server_capabilities = result.capabilities
        await self.send_notification(mcp_types.ClientNotification(mcp_types.InitializedNotification()))
        return result


def test_quality_apps_register_exact_resources_and_existing_tool_bindings(tmp_path: Path) -> None:
    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path, log_level="CRITICAL"))
        await application.start()
        try:
            plugins = application.services.get("plugins")
            assert isinstance(plugins, PluginManager)
            resources = {item.uri: item for item in plugins.apps.resources()}
            assert set(resources).issuperset({QUALITY_OVERVIEW_APP_URI, QUALITY_FINDINGS_APP_URI})
            for uri in (QUALITY_OVERVIEW_APP_URI, QUALITY_FINDINGS_APP_URI):
                resource = resources[uri]
                assert resource.html.startswith("<!doctype html>")
                assert resource.csp.as_dict() == {
                    "connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": [],
                }
                assert resource.permissions == () and resource.domain is None and resource.prefers_border is True
            bindings = {item.tool_name: item for item in plugins.apps.bindings()}
            assert {name for name, item in bindings.items() if item.resource_uri == QUALITY_OVERVIEW_APP_URI} == _OVERVIEW_TOOLS
            assert {name for name, item in bindings.items() if item.resource_uri == QUALITY_FINDINGS_APP_URI} == _FINDINGS_TOOLS
            assert all(item.visibility == ("model", "app") for item in bindings.values() if item.tool_name in _OVERVIEW_TOOLS | _FINDINGS_TOOLS)
            assert _OVERVIEW_TOOLS | _FINDINGS_TOOLS <= {item.name for item in plugins.tools.contributions()}
        finally:
            await application.aclose()

    asyncio.run(exercise())


def test_quality_assets_are_packaged_bounded_and_authored_sources_are_read_only() -> None:
    sources = (
        _ROOT / "frontend" / "quality-apps" / "quality-overview-app.js",
        _ROOT / "frontend" / "quality-apps" / "quality-findings-app.js",
    )
    helper = (_ROOT / "frontend" / "common" / "mcp-app.js").read_text(encoding="utf-8")
    forbidden = (
        "fetch(", "XMLHttpRequest", "WebSocket", "innerHTML", "outerHTML", "insertAdjacentHTML",
        "document.write", "callServerTool", "tools/call", "resources/read", "localStorage",
        "sessionStorage", "eval(", "Function(", "ui/open-link", "requestDisplayMode",
    )
    for source_path in sources:
        source = source_path.read_text(encoding="utf-8")
        assert "textContent" in source and "safeText" in source
        assert all(token not in source + helper for token in forbidden)
    for asset_name in ("quality-overview.html", "quality-findings.html"):
        asset = files("forgemcp.apps.assets").joinpath(asset_name).read_text(encoding="utf-8")
        assert asset.startswith("<!doctype html>")
        assert "source-sha256:" in asset
        assert len(asset.encode("utf-8")) < 768 * 1024
        assert MCP_APP_HTML_MIME_TYPE == "text/html;profile=mcp-app"


def test_quality_app_metadata_is_connection_scoped_and_preserves_tool_fallback(tmp_path: Path) -> None:
    async def inspect(session_type: type[ClientSession], apps_capable: bool) -> dict[str, object]:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "forgemcp.server"],
            cwd=_ROOT,
            env={**os.environ, "FORGEMCP_WORKSPACE": str(tmp_path), "FORGEMCP_LOG_LEVEL": "CRITICAL"},
        )
        async with stdio_client(parameters) as streams:
            async with session_type(*streams) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo == mcp_types.Implementation(name="ForgeMCP", version=__version__)
                tools = {item.name: item for item in (await session.list_tools()).tools}
                quality_tools = _OVERVIEW_TOOLS | _FINDINGS_TOOLS
                if not apps_capable:
                    assert all(tools[name].meta is None for name in quality_tools)
                else:
                    for name in _OVERVIEW_TOOLS:
                        assert tools[name].meta == {"ui": {"resourceUri": QUALITY_OVERVIEW_APP_URI, "visibility": ["model", "app"]}}
                    for name in _FINDINGS_TOOLS:
                        assert tools[name].meta == {"ui": {"resourceUri": QUALITY_FINDINGS_APP_URI, "visibility": ["model", "app"]}}
                    resources = {str(item.uri): item for item in (await session.list_resources()).resources}
                    for uri in (QUALITY_OVERVIEW_APP_URI, QUALITY_FINDINGS_APP_URI):
                        assert resources[uri].mimeType == MCP_APP_HTML_MIME_TYPE
                        assert resources[uri].meta == {"ui": {"csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": []}, "prefersBorder": True}}
                        content = await session.read_resource(mcp_types.AnyUrl(uri))
                        assert len(content.contents) == 1 and content.contents[0].text.startswith("<!doctype html>")
                return {
                    name: {"input": tools[name].inputSchema, "output": tools[name].outputSchema, "annotations": tools[name].annotations}
                    for name in sorted(quality_tools)
                }

    apps = asyncio.run(inspect(_AppsClientSession, True))
    plain = asyncio.run(inspect(ClientSession, False))
    assert apps == plain
