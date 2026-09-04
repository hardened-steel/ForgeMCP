"""Focused contribution and asset safety tests for the clangd result Apps."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

from forgemcp.clangd.plugin import (
    CLANGD_CHANGE_HIERARCHY_APP_URI,
    CLANGD_INSIGHT_APP_URI,
    CLANGD_NAVIGATION_APP_URI,
    CLANGD_SESSION_APP_URI,
    ClangdPlugin,
)
from forgemcp.plugins import AppRegistry, MCP_APP_HTML_MIME_TYPE
from forgemcp.plugins.apps import MAX_APP_HTML_BYTES


_ROOT = Path(__file__).parents[1]
_EXPECTED = {
    CLANGD_SESSION_APP_URI: {"clangd__status", "clangd__start", "clangd__stop"},
    CLANGD_INSIGHT_APP_URI: {"clangd__diagnostics", "clangd__hover", "clangd__completion", "clangd__signature_help"},
    CLANGD_NAVIGATION_APP_URI: {
        "clangd__definition", "clangd__references", "clangd__declaration", "clangd__type_definition",
        "clangd__implementation", "clangd__document_symbols", "clangd__workspace_symbols", "clangd__switch_source_header",
    },
    CLANGD_CHANGE_HIERARCHY_APP_URI: {
        "clangd__prepare_rename", "clangd__rename", "clangd__code_actions", "clangd__apply_code_action",
        "clangd__format_document", "clangd__format_range", "clangd__prepare_call_hierarchy", "clangd__incoming_calls",
        "clangd__outgoing_calls", "clangd__prepare_type_hierarchy", "clangd__supertypes", "clangd__subtypes",
    },
}


class _Apps:
    def __init__(self) -> None:
        self.registry = AppRegistry()

    def register_resource(self, resource: object) -> None:
        self.registry.register_resource("clangd", resource)  # type: ignore[arg-type]

    def bind_tool(self, binding: object) -> None:
        self.registry.register_tool_binding("clangd", binding)  # type: ignore[arg-type]


class _Tools:
    def __init__(self) -> None:
        self.names: list[str] = []

    def register(self, contribution: object) -> None:
        self.names.append(contribution.name)  # type: ignore[attr-defined]


def test_clangd_apps_bind_every_existing_clangd_tool_to_one_of_four_static_resources() -> None:
    apps = _Apps()
    tools = _Tools()
    context = SimpleNamespace(apps=apps, tools=tools)
    plugin = ClangdPlugin()
    plugin._register_tools(context)  # type: ignore[arg-type]
    plugin._register_apps(context)  # type: ignore[arg-type]

    public_tools = {f"clangd__{name}" for name in tools.names}
    assert len(public_tools) == 27
    apps.registry.validate(public_tools)
    assert {resource.uri for resource in apps.registry.resources()} == set(_EXPECTED)
    assert {binding.tool_name for binding in apps.registry.bindings()} == set().union(*_EXPECTED.values())
    for resource in apps.registry.resources():
        assert resource.resource_meta() == {
            "ui": {
                "csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": []},
                "prefersBorder": True,
            }
        }
        assert resource.html.startswith("<!doctype html>")
        assert len(resource.html.encode("utf-8")) <= MAX_APP_HTML_BYTES
    for uri, tool_names in _EXPECTED.items():
        for tool_name in tool_names:
            binding = apps.registry.binding_for(tool_name)
            assert binding is not None
            assert binding.resource_uri == uri
            assert binding.visibility == ("model", "app")


def test_clangd_assets_are_packaged_safe_and_do_not_add_ui_originated_mcp_calls() -> None:
    assets = ("clangd-session.html", "clangd-insight.html", "clangd-navigation.html", "clangd-change-hierarchy.html")
    sources = ("shared.js", "clangd-session-app.js", "clangd-insight-app.js", "clangd-navigation-app.js", "clangd-change-hierarchy-app.js")
    authored = "\n".join((_ROOT / "frontend" / "clangd-apps" / source).read_text(encoding="utf-8") for source in sources)
    for forbidden in (
        "callServerTool", "tools/call", "resources/read", "fetch(", "XMLHttpRequest", "WebSocket", "innerHTML",
        "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function(", "localStorage", "ui/open-link",
        "ui/update-model-context",
    ):
        assert forbidden not in authored
    assert "textContent" in authored
    assert "structuredContent" in authored and "JSON.parse" in authored
    for name in assets:
        html = files("forgemcp.apps.assets").joinpath(name).read_text(encoding="utf-8")
        assert html.startswith("<!doctype html>")
        assert "source-sha256:" in html
        assert len(html.encode("utf-8")) <= MAX_APP_HTML_BYTES
        assert MCP_APP_HTML_MIME_TYPE == "text/html;profile=mcp-app"


def test_clangd_result_apps_keep_tool_handlers_and_fallback_dispatch_unchanged() -> None:
    """Registration adds metadata only; the existing 27 tool contributions remain intact."""
    tools = _Tools()
    ClangdPlugin()._register_tools(SimpleNamespace(tools=tools))  # type: ignore[arg-type]
    assert tools.names == [
        "status", "start", "stop", "diagnostics", "hover", "definition", "references", "document_symbols",
        "workspace_symbols", "completion", "signature_help", "declaration", "type_definition", "implementation",
        "prepare_rename", "rename", "code_actions", "apply_code_action", "format_document", "format_range",
        "prepare_call_hierarchy", "incoming_calls", "outgoing_calls", "prepare_type_hierarchy", "supertypes",
        "subtypes", "switch_source_header",
    ]
