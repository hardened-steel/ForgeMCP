"""Transport-neutral MCP App contribution contracts.

Feature plugins use these immutable models without importing MCP, FastMCP, or
postMessage transport types.  The SDK projection remains in ``server.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from forgemcp.plugins.errors import (
    ContributionLimitError,
    DuplicateAppResourceUriError,
    DuplicateToolAppBindingError,
    MissingAppResourceError,
)


MCP_APPS_EXTENSION_ID = "io.modelcontextprotocol/ui"
MCP_APP_HTML_MIME_TYPE = "text/html;profile=mcp-app"
# Resources and bindings have independent bounded inventories.  A shared
# result-family resource is deliberately reused by many public tools, so the
# complete 72-tool surface needs more bindings than resources.
MAX_APP_RESOURCES = 32
MAX_APP_TOOL_BINDINGS = 128
# The official ext-apps runtime is bundled into each static, CSP-isolated App.
# Keep a finite resource cap while allowing the verified runtime itself.
MAX_APP_HTML_BYTES = 768 * 1024
MAX_APP_URI_CHARACTERS = 512
MAX_APP_DESCRIPTION_CHARACTERS = 1024
_VALID_VISIBILITY = frozenset(("model", "app"))
_VALID_PERMISSIONS = frozenset(("camera", "microphone", "geolocation", "clipboardWrite"))


def _bounded_text(value: object, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded non-empty string.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} must not contain control characters.")
    return value


def _bounded_html(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("App HTML must be a bounded non-empty string.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("App HTML must be UTF-8 encodable.") from error
    if len(encoded) > MAX_APP_HTML_BYTES:
        raise ValueError("App HTML exceeds the configured byte limit.")
    if "\x00" in value:
        raise ValueError("App HTML must not contain NUL characters.")
    return value


def _normalise_domains(values: object, *, label: str) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError(f"{label} must be a collection of origins.")
    try:
        domains = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"{label} must be a collection of origins.") from error
    if len(domains) > 32 or len(domains) != len(set(domains)):
        raise ValueError(f"{label} must be unique and bounded.")
    for domain in domains:
        text = _bounded_text(domain, label=label, maximum=512)
        if "://" not in text:
            raise ValueError(f"{label} entries must be absolute origins.")
    return domains


@dataclass(frozen=True, slots=True)
class AppCsp:
    """CSP allow-lists for an App resource, independent of a web host."""

    connect_domains: tuple[str, ...] = ()
    resource_domains: tuple[str, ...] = ()
    frame_domains: tuple[str, ...] = ()
    base_uri_domains: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "connect_domains", _normalise_domains(self.connect_domains, label="CSP connect domains")
        )
        object.__setattr__(
            self, "resource_domains", _normalise_domains(self.resource_domains, label="CSP resource domains")
        )
        object.__setattr__(
            self, "frame_domains", _normalise_domains(self.frame_domains, label="CSP frame domains")
        )
        object.__setattr__(
            self, "base_uri_domains", _normalise_domains(self.base_uri_domains, label="CSP base URI domains")
        )

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "connectDomains": list(self.connect_domains),
            "resourceDomains": list(self.resource_domains),
            "frameDomains": list(self.frame_domains),
            "baseUriDomains": list(self.base_uri_domains),
        }


@dataclass(frozen=True, slots=True)
class AppResourceContribution:
    """One immutable static HTML MCP App resource."""

    uri: str
    name: str
    description: str
    html: str
    csp: AppCsp = AppCsp()
    permissions: tuple[str, ...] = ()
    domain: str | None = None
    prefers_border: bool = True

    def __post_init__(self) -> None:
        uri = _bounded_text(self.uri, label="App resource URI", maximum=MAX_APP_URI_CHARACTERS)
        if not uri.startswith("ui://") or "{" in uri or "}" in uri:
            raise ValueError("App resource URIs must use the static ui:// scheme.")
        _bounded_text(self.name, label="App resource name", maximum=128)
        _bounded_text(self.description, label="App resource description", maximum=MAX_APP_DESCRIPTION_CHARACTERS)
        html = _bounded_html(self.html)
        if not html.lstrip().lower().startswith("<!doctype html"):
            raise ValueError("App HTML must be a bounded HTML5 document.")
        if not isinstance(self.csp, AppCsp):
            raise TypeError("App CSP must be an AppCsp value.")
        permissions = tuple(self.permissions)
        if len(permissions) != len(set(permissions)) or any(item not in _VALID_PERMISSIONS for item in permissions):
            raise ValueError("App permissions must be unique supported permission names.")
        if self.domain is not None:
            domain = _bounded_text(self.domain, label="App domain", maximum=512)
            if "://" not in domain:
                raise ValueError("App domain must be an absolute origin.")
        if not isinstance(self.prefers_border, bool):
            raise TypeError("App prefers_border must be boolean.")
        object.__setattr__(self, "permissions", permissions)

    def resource_meta(self) -> dict[str, object]:
        ui: dict[str, object] = {"csp": self.csp.as_dict(), "prefersBorder": self.prefers_border}
        if self.permissions:
            ui["permissions"] = {permission: {} for permission in self.permissions}
        if self.domain is not None:
            ui["domain"] = self.domain
        return {"ui": ui}


@dataclass(frozen=True, slots=True)
class ToolAppBinding:
    """Bind one existing model-visible tool to one registered App resource."""

    tool_name: str
    resource_uri: str
    visibility: tuple[Literal["model", "app"], ...] = ("model", "app")

    def __post_init__(self) -> None:
        tool_name = _bounded_text(self.tool_name, label="App binding tool name", maximum=128)
        # ``server_status`` predates feature namespaces and is the one stable
        # Core diagnostic tool retained for backwards compatibility.  Every
        # other App-bound public tool remains namespace-qualified.
        if tool_name != "server_status" and (
            "__" not in tool_name
            or not all(part.replace("_", "").isalnum() for part in tool_name.split("__"))
        ):
            raise ValueError("App binding tool names must be qualified ForgeMCP tool names.")
        uri = _bounded_text(self.resource_uri, label="App binding resource URI", maximum=MAX_APP_URI_CHARACTERS)
        if not uri.startswith("ui://"):
            raise ValueError("App bindings must reference ui:// resources.")
        visibility = tuple(self.visibility)
        if not visibility or len(visibility) != len(set(visibility)) or not set(visibility) <= _VALID_VISIBILITY:
            raise ValueError("App visibility must be a unique non-empty model/app collection.")
        object.__setattr__(self, "visibility", visibility)

    def tool_meta(self) -> dict[str, object]:
        return {"ui": {"resourceUri": self.resource_uri, "visibility": list(self.visibility)}}


@dataclass(frozen=True, slots=True)
class _OwnedAppResource:
    plugin_id: str
    contribution: AppResourceContribution


@dataclass(frozen=True, slots=True)
class _OwnedToolBinding:
    plugin_id: str
    binding: ToolAppBinding


class AppRegistry:
    """Application-owned bounded registry for static MCP App resources and bindings."""

    def __init__(self) -> None:
        self._resources: dict[str, _OwnedAppResource] = {}
        self._bindings: dict[str, _OwnedToolBinding] = {}
        self._closed = False

    def register_resource(self, plugin_id: str, contribution: AppResourceContribution) -> None:
        self._ensure_open()
        if len(self._resources) >= MAX_APP_RESOURCES:
            raise ContributionLimitError("MCP App resource contribution limit exceeded.")
        if contribution.uri in self._resources:
            raise DuplicateAppResourceUriError(f"MCP App URI already registered: {contribution.uri}")
        self._resources[contribution.uri] = _OwnedAppResource(plugin_id, contribution)

    def register_tool_binding(self, plugin_id: str, binding: ToolAppBinding) -> None:
        self._ensure_open()
        if len(self._bindings) >= MAX_APP_TOOL_BINDINGS:
            raise ContributionLimitError("MCP App tool binding limit exceeded.")
        if binding.tool_name in self._bindings:
            raise DuplicateToolAppBindingError(f"MCP App binding already registered: {binding.tool_name}")
        self._bindings[binding.tool_name] = _OwnedToolBinding(plugin_id, binding)

    def validate(self, tool_names: Iterable[str]) -> None:
        available_tools = frozenset(tool_names)
        for tool_name, owned in self._bindings.items():
            if tool_name not in available_tools:
                raise MissingAppResourceError(f"MCP App binding references missing tool: {tool_name}")
            if owned.binding.resource_uri not in self._resources:
                raise MissingAppResourceError("MCP App binding references missing UI resource.")

    def resources(self) -> tuple[AppResourceContribution, ...]:
        return tuple(self._resources[key].contribution for key in sorted(self._resources))

    def binding_for(self, tool_name: str) -> ToolAppBinding | None:
        owned = self._bindings.get(tool_name)
        return None if owned is None else owned.binding

    def bindings(self) -> tuple[ToolAppBinding, ...]:
        return tuple(self._bindings[key].binding for key in sorted(self._bindings))

    def unregister_plugin(self, plugin_id: str) -> None:
        for registry in (self._resources, self._bindings):
            for key in tuple(registry):
                if registry[key].plugin_id == plugin_id:
                    del registry[key]

    async def aclose(self) -> None:
        self._closed = True
        self._resources.clear()
        self._bindings.clear()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ContributionLimitError("MCP App registry is closed.")


class PluginAppRegistry:
    """Plugin-scoped facade which preserves contribution ownership."""

    __slots__ = ("_plugin_id", "_registry")

    def __init__(self, plugin_id: str, registry: AppRegistry) -> None:
        self._plugin_id = plugin_id
        self._registry = registry

    def register_resource(self, contribution: AppResourceContribution) -> None:
        self._registry.register_resource(self._plugin_id, contribution)

    def bind_tool(self, binding: ToolAppBinding) -> None:
        self._registry.register_tool_binding(self._plugin_id, binding)
