"""Focused packaging and binding checks for the debugger MCP Apps."""

from __future__ import annotations

from importlib.resources import files
from types import SimpleNamespace

from forgemcp.debugger.plugin import (
    DEBUGGER_DATA_APP_URI,
    DEBUGGER_SESSION_APP_URI,
    DEBUGGER_STACK_APP_URI,
    DebuggerPlugin,
)
from forgemcp.plugins import AppRegistry, MCP_APP_HTML_MIME_TYPE, PluginAppRegistry


_MAPPING = {
    DEBUGGER_SESSION_APP_URI: (
        "debugger__status", "debugger__list_adapters", "debugger__launch", "debugger__stop",
        "debugger__set_breakpoints", "debugger__continue", "debugger__pause", "debugger__step_over",
        "debugger__step_in", "debugger__step_out", "debugger__events",
    ),
    DEBUGGER_STACK_APP_URI: ("debugger__threads", "debugger__stack_trace"),
    DEBUGGER_DATA_APP_URI: ("debugger__scopes", "debugger__variables", "debugger__evaluate"),
}


def test_debugger_app_resources_have_unique_bindings_and_restrictive_metadata() -> None:
    registry = AppRegistry()
    DebuggerPlugin._register_apps(SimpleNamespace(apps=PluginAppRegistry("debugger", registry)))
    all_tools = tuple(tool_name for names in _MAPPING.values() for tool_name in names)
    registry.validate(all_tools)
    assert len(registry.resources()) == 3
    assert len(registry.bindings()) == 16
    assert {resource.uri for resource in registry.resources()} == set(_MAPPING)
    assert all(resource.resource_meta() == {"ui": {"csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": []}, "prefersBorder": True}} for resource in registry.resources())
    assert all(binding.visibility == ("model", "app") and binding.resource_uri in _MAPPING for binding in registry.bindings())
    assert MCP_APP_HTML_MIME_TYPE == "text/html;profile=mcp-app"


def test_debugger_assets_are_packaged_and_authored_sources_remain_safe() -> None:
    root = files("forgemcp.apps.assets")
    for name in ("debugger-session.html", "debugger-stack.html", "debugger-data.html"):
        html = root.joinpath(name).read_text(encoding="utf-8")
        assert html.startswith("<!doctype html><!-- source-sha256:")
        assert len(html.encode("utf-8")) < 768 * 1024
        assert "forgemcp-debugger-" in html
