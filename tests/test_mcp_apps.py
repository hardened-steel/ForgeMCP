"""Protocol, packaging, and safety gates for the read-only Git Status MCP App."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import zipfile
from importlib.util import find_spec
from importlib.resources import files
from pathlib import Path

import pytest
from mcp import ClientSession, types as mcp_types
from mcp.client.stdio import StdioServerParameters, stdio_client

from forgemcp import __version__
from forgemcp.git.plugin import GIT_STATUS_APP_URI
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
from forgemcp.server import client_supports_mcp_apps
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
        _app_resource("<!doctype html>" + "x" * (256 * 1024))


def test_client_capability_negotiation_requires_the_exact_html_mime_type() -> None:
    assert client_supports_mcp_apps(_APPS_CAPABILITY) is True
    assert client_supports_mcp_apps({"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/plain"]}}}) is False
    assert client_supports_mcp_apps({"experimental": {"io.modelcontextprotocol/ui": {}}}) is False
    assert client_supports_mcp_apps(mcp_types.ClientCapabilities.model_validate(_APPS_CAPABILITY)) is True
    assert MCP_APP_RESOURCE_INVENTORY == (
        {"tool_name": "git__status", "uri": GIT_STATUS_APP_URI, "mime_type": MCP_APP_HTML_MIME_TYPE},
    )


def test_git_status_asset_is_static_safe_and_loaded_from_the_package() -> None:
    html = files("forgemcp.apps.assets").joinpath("git-status.html").read_text(encoding="utf-8")
    source = (_ROOT / "frontend" / "git-status" / "git-status-app.js").read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert len(html.encode("utf-8")) <= 256 * 1024
    assert "source-sha256:" in html
    assert "C:\\" not in html and "/Users/" not in html
    assert "fetch(" not in html and "XMLHttpRequest" not in html and "WebSocket" not in html
    assert "innerHTML" not in html and "insertAdjacentHTML" not in html and "document.write" not in html
    assert "localStorage" not in html and "ui/open-link" not in html and "ui/update-model-context" not in html
    assert "resources/read" not in html
    assert "textContent" in source
    assert "display(status.branch)" in source and "display(file.path)" in source
    assert "TOOL_NAME = \"git__status\"" in source and "tools/call" in source


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
    assert "row.append(element" in source
    assert "textContent = text" in source
    assert "replace(/[" in source


def test_sdk_stdio_apps_and_no_apps_contracts(tmp_path: Path) -> None:
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
                assert initialized.capabilities.experimental is None
                extensions = getattr(initialized.capabilities, "extensions", None)
                assert extensions == {"io.modelcontextprotocol/ui": {}}

                tools = await session.list_tools()
                assert len(tools.tools) == 72
                status_tool = next(tool for tool in tools.tools if tool.name == "git__status")
                if apps_capable:
                    assert status_tool.meta == {
                        "ui": {"resourceUri": GIT_STATUS_APP_URI, "visibility": ["model", "app"]}
                    }
                    resources = await session.list_resources()
                    resource = next(item for item in resources.resources if str(item.uri) == GIT_STATUS_APP_URI)
                    assert resource.mimeType == MCP_APP_HTML_MIME_TYPE
                    assert resource.meta == {
                        "ui": {
                            "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": []},
                            "prefersBorder": True,
                        }
                    }
                    content = await session.read_resource(mcp_types.AnyUrl(GIT_STATUS_APP_URI))
                    assert len(content.contents) == 1
                    item = content.contents[0]
                    assert str(item.uri) == GIT_STATUS_APP_URI
                    assert item.mimeType == MCP_APP_HTML_MIME_TYPE
                    assert item.meta == resource.meta
                    assert item.text.startswith("<!doctype html>")
                else:
                    assert status_tool.meta is None
                    assert (await session.list_resources()).resources

                result = await session.call_tool("git__status", {})
                assert result.isError is False
                assert len(result.content) == 1
                payload = json.loads(result.content[0].text)
                assert isinstance(payload, dict)
                assert result.structuredContent == payload
                return {
                    "input_schema": status_tool.inputSchema,
                    "output_schema": status_tool.outputSchema,
                    "annotations": status_tool.annotations,
                    "result": result.model_dump(by_alias=True),
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
            "from importlib.resources import files; value=files('forgemcp.apps.assets').joinpath('git-status.html').read_text(encoding='utf-8'); "
            "assert value.startswith('<!doctype html>'); print(len(value))",
            str(target),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert smoke.returncode == 0, smoke.stderr
