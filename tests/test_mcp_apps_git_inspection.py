"""Focused registration, packaging, and safety checks for Git inspection Apps."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

from forgemcp.git.plugin import (
    GIT_DIFF_APP_URI,
    GIT_HISTORY_APP_URI,
    GIT_SOURCE_HISTORY_APP_URI,
    GitPlugin,
)
from forgemcp.plugins import (
    AppRegistry,
    MCP_APP_HTML_MIME_TYPE,
    PluginAppRegistry,
)
from forgemcp.plugins.apps import MAX_APP_HTML_BYTES


_ROOT = Path(__file__).parents[1]
_RESOURCE_BINDINGS = {
    "git__diff": GIT_DIFF_APP_URI,
    "git__log": GIT_HISTORY_APP_URI,
    "git__list_branches": GIT_HISTORY_APP_URI,
    "git__show_commit": GIT_SOURCE_HISTORY_APP_URI,
    "git__blame": GIT_SOURCE_HISTORY_APP_URI,
}
_ASSETS = {
    GIT_DIFF_APP_URI: "git-diff.html",
    GIT_HISTORY_APP_URI: "git-history.html",
    GIT_SOURCE_HISTORY_APP_URI: "git-source-history.html",
}


def _registry() -> AppRegistry:
    registry = AppRegistry()
    context = SimpleNamespace(apps=PluginAppRegistry("git", registry))
    GitPlugin._register_inspection_apps(context)  # type: ignore[arg-type]
    registry.validate(_RESOURCE_BINDINGS)
    return registry


def test_git_inspection_tools_bind_to_three_unique_static_resources() -> None:
    registry = _registry()
    resources = {resource.uri: resource for resource in registry.resources()}

    assert set(resources) == set(_ASSETS)
    assert len(resources) == 3
    for tool_name, uri in _RESOURCE_BINDINGS.items():
        binding = registry.binding_for(tool_name)
        assert binding is not None
        assert binding.resource_uri == uri
        assert binding.visibility == ("model", "app")
    for resource in resources.values():
        assert resource.prefers_border is True
        assert resource.csp.as_dict() == {
            "connectDomains": [], "resourceDomains": [], "frameDomains": [], "baseUriDomains": [],
        }
        assert resource.permissions == () and resource.domain is None


def test_git_inspection_assets_are_packaged_html_with_the_exact_app_mime_contract() -> None:
    assert MCP_APP_HTML_MIME_TYPE == "text/html;profile=mcp-app"
    for name in _ASSETS.values():
        html = files("forgemcp.apps.assets").joinpath(name).read_text(encoding="utf-8")
        assert html.startswith("<!doctype html><!-- source-sha256:")
        assert len(html.encode("utf-8")) <= MAX_APP_HTML_BYTES
        assert "C:\\" not in html and "/Users/" not in html


def test_git_inspection_frontends_handle_public_shapes_without_unsafe_dom_or_network_calls() -> None:
    directory = _ROOT / "frontend" / "git-inspection-apps"
    sources = {
        name: (directory / name).read_text(encoding="utf-8")
        for name in ("git-diff-app.js", "git-history-app.js", "git-source-history-app.js")
    }
    authored = "\n".join(sources.values())
    for forbidden in (
        "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "callServerTool", "tools/call",
        "resources/read", "fetch(", "XMLHttpRequest", "WebSocket", "localStorage", "eval(", "Function(",
        "window.parent.postMessage",
    ):
        assert forbidden not in authored
    assert "textContent" in authored
    assert "structuredContent" in authored
    assert "JSON.parse" in authored  # The existing one-item JSON text fallback.
    assert "value.patch" in sources["git-diff-app.js"]
    assert "document.createTextNode(\"\\n\")" in sources["git-diff-app.js"]
    assert "value.commits" in sources["git-history-app.js"]
    assert "value.branches" in sources["git-history-app.js"]
    assert "value.ranges" in sources["git-source-history-app.js"]
    assert "value.patch" in sources["git-source-history-app.js"]
    for name in ("forgemcp-git-diff", "forgemcp-git-history", "forgemcp-git-source-history"):
        assert name in authored


def test_git_inspection_build_sources_cover_bounded_representative_result_fields() -> None:
    """Keep validation coupled to public Git models, not internal Git process data."""
    directory = _ROOT / "frontend" / "git-inspection-apps"
    diff = (directory / "git-diff-app.js").read_text(encoding="utf-8")
    history = (directory / "git-history-app.js").read_text(encoding="utf-8")
    source_history = (directory / "git-source-history-app.js").read_text(encoding="utf-8")

    for field in ("summary.scope", "summary.patch_truncated", "summary.binary_file_count", "summary.incomplete"):
        assert field in diff
    for field in ("oid", "subject", "author_name", "authored_at", "current", "upstream", "ahead", "behind"):
        assert field in history
    for field in ("commit", "patch_truncated", "binary_file_count", "path", "ranges", "start_line", "end_line"):
        assert field in source_history
    assert "gitdir" not in "\n".join((diff, history, source_history)).lower()
