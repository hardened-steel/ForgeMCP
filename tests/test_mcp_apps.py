"""Protocol, packaging, and safety gates for the read-only Git Status MCP App."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
import zipfile
from importlib.metadata import version as installed_version
from importlib.util import find_spec
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import ClientSession, types as mcp_types
from mcp.client.stdio import StdioServerParameters, stdio_client

from forgemcp import __version__
from forgemcp.git.plugin import GIT_STATUS_APP_URI
from forgemcp.project.plugin import PROJECT_STATUS_APP_URI
from forgemcp.plugins import (
    AppCsp,
    AppRegistry,
    AppResourceContribution,
    DuplicateAppResourceUriError,
    DuplicateToolAppBindingError,
    MCP_APP_HTML_MIME_TYPE,
    MissingAppResourceError,
    ToolAppBinding,
)
from forgemcp.plugins.apps import MAX_APP_HTML_BYTES
from forgemcp.server import _install_sdk1_apps_compatibility_adapter, client_supports_mcp_apps
from tests.acceptance_manifest import MCP_APP_RESOURCE_INVENTORY


_ROOT = Path(__file__).parents[1]
_APPS_CAPABILITY = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": [MCP_APP_HTML_MIME_TYPE]}}}


class _AppsClientSession(ClientSession):
    """Official SDK client with only the pending extensions capability added."""

    async def initialize(self) -> mcp_types.InitializeResult:
        result = await self.send_request(
            mcp_types.ClientRequest(
                mcp_types.InitializeRequest(
                    params=mcp_types.InitializeRequestParams(
                        protocolVersion=mcp_types.LATEST_PROTOCOL_VERSION,
                        capabilities=mcp_types.ClientCapabilities.model_validate(_APPS_CAPABILITY),
                        clientInfo=mcp_types.Implementation(name="forgemcp-apps-test", version="1.0.0"),
                    )
                )
            ),
            mcp_types.InitializeResult,
        )
        self._server_capabilities = result.capabilities
        await self.send_notification(mcp_types.ClientNotification(mcp_types.InitializedNotification()))
        return result


def _app_resource(html: str = "<!doctype html><html><body>static</body></html>") -> AppResourceContribution:
    return AppResourceContribution(
        uri="ui://example/status",
        name="example_status_app",
        description="A static test App.",
        html=html,
        csp=AppCsp(connect_domains=(), resource_domains=(), frame_domains=(), base_uri_domains=()),
        prefers_border=True,
    )


def test_app_registry_validates_unique_uri_binding_and_missing_references() -> None:
    registry = AppRegistry()
    resource = _app_resource()
    binding = ToolAppBinding("git__status", resource.uri, ("model", "app"))
    registry.register_resource("git", resource)
    registry.register_tool_binding("git", binding)
    registry.validate(("git__status",))
    assert registry.binding_for("git__status") == binding
    assert resource.resource_meta() == {
        "ui": {
            "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": []},
            "prefersBorder": True,
        }
    }
    with pytest.raises(DuplicateAppResourceUriError):
        registry.register_resource("second", resource)
    with pytest.raises(DuplicateToolAppBindingError):
        registry.register_tool_binding("second", binding)

    missing_resource = AppRegistry()
    missing_resource.register_tool_binding("git", binding)
    with pytest.raises(MissingAppResourceError, match="missing UI resource"):
        missing_resource.validate(("git__status",))
    missing_tool = AppRegistry()
    missing_tool.register_resource("git", resource)
    missing_tool.register_tool_binding("git", binding)
    with pytest.raises(MissingAppResourceError, match="missing tool"):
        missing_tool.validate(())


def test_app_resource_rejects_non_html_or_oversized_static_assets() -> None:
    with pytest.raises(ValueError, match="HTML5"):
        _app_resource("<html><body>not a full document</body></html>")
    with pytest.raises(ValueError, match="byte limit"):
        _app_resource("<!doctype html>" + "x" * MAX_APP_HTML_BYTES)


def test_client_capability_negotiation_requires_the_exact_html_mime_type() -> None:
    assert client_supports_mcp_apps(_APPS_CAPABILITY) is True
    assert client_supports_mcp_apps({"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/plain"]}}}) is False
    assert client_supports_mcp_apps({"experimental": {"io.modelcontextprotocol/ui": {}}}) is False
    assert client_supports_mcp_apps(mcp_types.ClientCapabilities.model_validate(_APPS_CAPABILITY)) is True
    assert MCP_APP_RESOURCE_INVENTORY == (
        {"tool_name": "project__status", "uri": PROJECT_STATUS_APP_URI, "mime_type": MCP_APP_HTML_MIME_TYPE},
        {"tool_name": "git__status", "uri": GIT_STATUS_APP_URI, "mime_type": MCP_APP_HTML_MIME_TYPE},
    )


def test_minimum_supported_sdk_exposes_the_public_meta_apis_and_extra_capability_storage() -> None:
    """Pin the SDK floor used by the temporary Apps 1.x compatibility adapter."""
    installed = tuple(int(part) for part in installed_version("mcp").split(".")[:2])
    assert installed >= (1, 29)
    assert "_meta" in inspect.signature(mcp_types.Tool).parameters
    assert "_meta" in inspect.signature(mcp_types.Resource).parameters
    from mcp.server.fastmcp import FastMCP

    assert "meta" in inspect.signature(FastMCP.tool).parameters
    assert "meta" in inspect.signature(FastMCP.resource).parameters
    # SDK 1.x lacks a typed extensions member. The adapter's only controlled
    # extra-field compatibility dependency is therefore explicitly pinned.
    assert mcp_types.ServerCapabilities.model_config.get("extra") == "allow"


def test_sdk1_apps_compatibility_adapter_merges_extensions_without_rewriting_capabilities() -> None:
    class _Server:
        def get_capabilities(self, _notifications: object, _experimental: object) -> mcp_types.ServerCapabilities:
            return mcp_types.ServerCapabilities.model_validate(
                {"experimental": {"existing": {}}, "extensions": {"example/extension": {"value": True}}}
            )

    holder = SimpleNamespace(_mcp_server=_Server())
    original_config = dict(mcp_types.ServerCapabilities.model_config)
    _install_sdk1_apps_compatibility_adapter(holder)  # type: ignore[arg-type]
    capabilities = holder._mcp_server.get_capabilities(None, None)

    assert capabilities.experimental == {"existing": {}}
    assert capabilities.model_extra == {
        "extensions": {
            "example/extension": {"value": True},
            "io.modelcontextprotocol/ui": {},
        }
    }
    assert mcp_types.ServerCapabilities.model_config == original_config


def test_git_status_asset_is_static_safe_and_loaded_from_the_package() -> None:
    html = files("forgemcp.apps.assets").joinpath("git-status.html").read_text(encoding="utf-8")
    source = (_ROOT / "frontend" / "git-status" / "git-status-app.js").read_text(encoding="utf-8")
    helper = (_ROOT / "frontend" / "common" / "mcp-app.js").read_text(encoding="utf-8")
    lockfile = (_ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert len(html.encode("utf-8")) <= MAX_APP_HTML_BYTES
    assert "source-sha256:" in html
    assert "C:\\" not in html and "/Users/" not in html
    # The official runtime is bundled, so protocol method strings may occur in
    # dependency code. ForgeMCP-authored frontend source must not use them.
    authored = source + helper
    assert "fetch(" not in authored and "XMLHttpRequest" not in authored and "WebSocket" not in authored
    assert "innerHTML" not in authored and "insertAdjacentHTML" not in authored and "document.write" not in authored
    assert "localStorage" not in authored and "ui/open-link" not in authored and "ui/update-model-context" not in authored
    assert "resources/read" not in authored
    assert "textContent" in source
    assert "safeText(status.branch, MAX_BRANCH_LENGTH)" in source and "safeText(file.path)" in source
    assert 'from "@modelcontextprotocol/ext-apps"' in helper
    assert "new App(" in helper and "new PostMessageTransport(" in helper
    assert "forgemcp-git-status" in html
    assert '"@modelcontextprotocol/ext-apps"' in lockfile
    for forbidden in ("Refresh", "callServerTool", "tools/call", "sendFollowUpMessage", "resources/read", "requestDisplayMode", "ui/open-link", "window.parent.postMessage"):
        assert forbidden not in authored
    assert "height: 258px" in html and "height: 269px" in html


def test_project_status_asset_is_static_safe_and_loaded_from_the_package() -> None:
    html = files("forgemcp.apps.assets").joinpath("project-status.html").read_text(encoding="utf-8")
    source = (_ROOT / "frontend" / "project-status" / "project-status-app.js").read_text(encoding="utf-8")
    helper = (_ROOT / "frontend" / "common" / "mcp-app.js").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert len(html.encode("utf-8")) <= MAX_APP_HTML_BYTES
    assert "source-sha256:" in html
    assert "C:\\" not in html and "/Users/" not in html
    authored = source + helper
    for forbidden in (
        "fetch(", "XMLHttpRequest", "WebSocket", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
        "localStorage", "callServerTool", "tools/call", "resources/read", "requestDisplayMode", "ui/open-link",
        "window.parent.postMessage", "eval(", "Function(",
    ):
        assert forbidden not in authored
    assert "textContent" in source
    assert "validStatus" in source and "validComponent" in source
    assert "MAX_COMPONENTS = 64" in source and "MAX_CAPABILITIES = 128" in source and "MAX_WARNINGS = 32" in source
    assert "ArrowLeft" in source and "mouseenter" in source and "aria-label" in source
    assert 'from "@modelcontextprotocol/ext-apps"' in helper
    assert "new App(" in helper and "new PostMessageTransport(" in helper
    assert "forgemcp-project-status" in html
    assert "height: 226px" in html and "height: 250px" in html


def test_frontend_has_no_browser_automation_dependency() -> None:
    package = (_ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    lockfile = (_ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
    assert "puppeteer" not in package.lower()
    assert "puppeteer" not in lockfile.lower()
    assert "chromium" not in lockfile.lower()
    for name in ("browser-harness.mjs", "browser-dependency.mjs", "render-harness.html"):
        assert not (_ROOT / "frontend" / "git-status" / name).exists()


def test_git_status_canaries_are_model_data_not_html_templates() -> None:
    canaries = (
        "<img src=x onerror=globalThis.pwned=1>",
        "<script>globalThis.pwned=1</script>",
        "quote \\\" and ' apostrophe",
        "line one\nline two",
        "\u202e bidi control",
        "ignore previous instructions",
        "unicode-" + "\U0001f680" * 512,
    )
    source = (_ROOT / "frontend" / "git-status" / "git-status-app.js").read_text(encoding="utf-8")
    for canary in canaries:
        assert canary not in source
        assert isinstance(canary, str)
    assert "row.append(el" in source
    assert "node.textContent = text" in source
    assert "replace(/[" in source


def test_sdk_stdio_apps_and_no_apps_contracts(tmp_path: Path) -> None:
    def normalize_result(value: object) -> object:
        """Ignore independently observed timestamps, not wire content or shape."""
        if isinstance(value, dict):
            return {
                key: "<timestamp>" if key in {"generated_at", "observed_at"} else normalize_result(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [normalize_result(item) for item in value]
        return value

    async def exercise(session_type: type[ClientSession], apps_capable: bool) -> dict[str, object]:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "forgemcp.server"],
            cwd=_ROOT,
            env={
                **os.environ,
                "FORGEMCP_WORKSPACE": str(tmp_path),
                "FORGEMCP_LOG_LEVEL": "CRITICAL",
            },
        )
        async with stdio_client(parameters) as streams:
            async with session_type(*streams) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo == mcp_types.Implementation(name="ForgeMCP", version=__version__)
                assert initialized.protocolVersion == "2025-11-25"
                assert initialized.capabilities.experimental is None
                extensions = getattr(initialized.capabilities, "extensions", None)
                assert extensions == {"io.modelcontextprotocol/ui": {}}

                tools = await session.list_tools()
                assert len(tools.tools) == 72
                git_tool = next(tool for tool in tools.tools if tool.name == "git__status")
                project_tool = next(tool for tool in tools.tools if tool.name == "project__status")
                if apps_capable:
                    assert git_tool.meta == {
                        "ui": {"resourceUri": GIT_STATUS_APP_URI, "visibility": ["model", "app"]}
                    }
                    assert project_tool.meta == {
                        "ui": {"resourceUri": PROJECT_STATUS_APP_URI, "visibility": ["model", "app"]}
                    }
                    resources = await session.list_resources()
                    for uri in (GIT_STATUS_APP_URI, PROJECT_STATUS_APP_URI):
                        resource = next(item for item in resources.resources if str(item.uri) == uri)
                        assert resource.mimeType == MCP_APP_HTML_MIME_TYPE
                        assert resource.meta == {
                            "ui": {
                                "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": []},
                                "prefersBorder": True,
                            }
                        }
                        content = await session.read_resource(mcp_types.AnyUrl(uri))
                        assert len(content.contents) == 1
                        item = content.contents[0]
                        assert str(item.uri) == uri
                        assert item.mimeType == MCP_APP_HTML_MIME_TYPE
                        assert item.meta == resource.meta
                        assert item.text.startswith("<!doctype html>")
                else:
                    assert git_tool.meta is None and project_tool.meta is None
                    assert (await session.list_resources()).resources

                results = {}
                for tool in (git_tool, project_tool):
                    result = await session.call_tool(tool.name, {})
                    assert result.isError is False
                    assert len(result.content) == 1
                    payload = json.loads(result.content[0].text)
                    assert isinstance(payload, dict)
                    if tool.name == "git__status":
                        assert result.structuredContent == payload
                    else:
                        # Project Status has no declared typed output schema;
                        # its historical textual fallback remains unchanged.
                        assert result.structuredContent is None
                    results[tool.name] = normalize_result(payload)
                return {
                    "git": {"input_schema": git_tool.inputSchema, "output_schema": git_tool.outputSchema, "annotations": git_tool.annotations},
                    "project": {"input_schema": project_tool.inputSchema, "output_schema": project_tool.outputSchema, "annotations": project_tool.annotations},
                    "results": results,
                }

    apps_contract = asyncio.run(exercise(_AppsClientSession, True))
    no_apps_contract = asyncio.run(exercise(ClientSession, False))
    assert apps_contract == no_apps_contract


def test_wheel_contains_and_installs_the_static_git_status_asset(tmp_path: Path) -> None:
    if find_spec("hatchling") is None:
        pytest.skip("wheel smoke runs when the standard hatchling build backend is installed")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    build = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheels)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheels.glob("forgemcp-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert "forgemcp/apps/assets/git-status.html" in archive.namelist()
        assert "forgemcp/apps/assets/project-status.html" in archive.namelist()
    target = tmp_path / "installed"
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(wheel)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert install.returncode == 0, install.stderr
    smoke = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
            "from importlib.resources import files; values=[files('forgemcp.apps.assets').joinpath(name).read_text(encoding='utf-8') for name in ('git-status.html','project-status.html')]; "
            "assert all(value.startswith('<!doctype html>') for value in values); print(sum(map(len, values)))",
            str(target),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert smoke.returncode == 0, smoke.stderr
