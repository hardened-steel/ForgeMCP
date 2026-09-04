"""Focused registrations and static-safety tests for the CMake result Apps."""
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

from forgemcp.cmake.plugin import CMAKE_CATALOG_APP_URI, CMAKE_OPERATION_APP_URI, CMakePlugin
from forgemcp.plugins import AppRegistry, MCP_APP_HTML_MIME_TYPE, PluginAppRegistry

ROOT = Path(__file__).parents[1]
CATALOG = ("cmake__status", "cmake__list_kits", "cmake__list_build_trees", "cmake__list_presets", "cmake__list_targets", "cmake__ctest_list_tests")
OPERATIONS = ("cmake__select_kit", "cmake__configure", "cmake__build", "cmake__ctest_run")

def test_cmake_apps_are_packaged_static_safe_and_fresh() -> None:
    assert MCP_APP_HTML_MIME_TYPE == "text/html;profile=mcp-app"
    for name, app_name in (("cmake-catalog.html", "forgemcp-cmake-catalog"), ("cmake-operation.html", "forgemcp-cmake-operation")):
        html = files("forgemcp.apps.assets").joinpath(name).read_text(encoding="utf-8")
        assert html.startswith("<!doctype html><!-- source-sha256:")
        assert app_name in html and len(html.encode("utf-8")) < 768 * 1024
    authored = "\n".join((ROOT / "frontend" / "cmake-apps" / name).read_text(encoding="utf-8") for name in ("catalog-app.js", "operation-app.js", "build.mjs"))
    for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "callServerTool", "tools/call", "resources/read", "eval(", "Function("):
        assert forbidden not in authored
    assert "textContent" in authored and "forgemcp-cmake-catalog" in authored and "forgemcp-cmake-operation" in authored

def test_cmake_app_tool_resource_mapping_is_complete_and_unique() -> None:
    mapping = {tool: CMAKE_CATALOG_APP_URI for tool in CATALOG} | {tool: CMAKE_OPERATION_APP_URI for tool in OPERATIONS}
    assert len(mapping) == 10 and set(mapping.values()) == {CMAKE_CATALOG_APP_URI, CMAKE_OPERATION_APP_URI}
    plugin = (ROOT / "src" / "forgemcp" / "cmake" / "plugin.py").read_text(encoding="utf-8")
    for tool in mapping:
        assert tool in plugin
    assert 'visibility=("model", "app")' in plugin

def test_cmake_app_registrations_have_exact_csp_and_each_requested_binding() -> None:
    registry = AppRegistry()
    CMakePlugin._register_apps(SimpleNamespace(apps=PluginAppRegistry("cmake", registry)))
    registry.validate((*CATALOG, *OPERATIONS))
    assert {resource.uri for resource in registry.resources()} == {CMAKE_CATALOG_APP_URI, CMAKE_OPERATION_APP_URI}
    for resource in registry.resources():
        assert resource.resource_meta() == {
            "ui": {"csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": []}, "prefersBorder": True}
        }
    for tool, uri in {tool: CMAKE_CATALOG_APP_URI for tool in CATALOG}.items() | {tool: CMAKE_OPERATION_APP_URI for tool in OPERATIONS}.items():
        assert registry.binding_for(tool).resource_uri == uri
        assert registry.binding_for(tool).visibility == ("model", "app")
