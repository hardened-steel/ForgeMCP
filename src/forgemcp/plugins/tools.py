"""Transport-neutral declarations and registry for feature-provided tools."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from forgemcp.plugins.errors import DuplicateToolNameError, ToolNamespaceError

_TOOL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_PLUGIN_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]*$")

ToolHandler = Callable[[Mapping[str, object]], object | Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ToolContribution:
    """One operation offered by a plugin, independent of an MCP SDK."""

    name: str
    description: str
    handler: ToolHandler = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _TOOL_IDENTIFIER.fullmatch(self.name):
            raise ToolNamespaceError(
                "Tool contribution names must be lower-case identifiers using letters, digits, and underscores."
            )
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Tool contribution descriptions must not be empty.")


@dataclass(frozen=True, slots=True)
class RegisteredToolContribution:
    """A contribution qualified with its owning plugin's stable namespace."""

    plugin_id: str
    contribution: ToolContribution

    @property
    def name(self) -> str:
        """Return the MCP-safe stable qualified name, e.g. ``cmake__configure``."""
        return f"{self.plugin_id}__{self.contribution.name}"

    @property
    def description(self) -> str:
        """Return the transport-neutral contribution description."""
        return self.contribution.description

    @property
    def handler(self) -> ToolHandler:
        """Return the contribution handler without exposing a transport object."""
        return self.contribution.handler


class ToolRegistry:
    """Application-owned registry that rejects duplicate qualified tool names."""

    def __init__(self) -> None:
        self._contributions: dict[str, RegisteredToolContribution] = {}

    def register(self, plugin_id: str, contribution: ToolContribution) -> RegisteredToolContribution:
        """Register one contribution in a plugin-owned stable namespace."""
        if not isinstance(plugin_id, str) or not _PLUGIN_IDENTIFIER.fullmatch(plugin_id):
            raise ToolNamespaceError(
                "Tool namespaces must be lower-case plugin identifiers using letters, digits, hyphens, and underscores."
            )
        registered = RegisteredToolContribution(plugin_id=plugin_id, contribution=contribution)
        if registered.name in self._contributions:
            raise DuplicateToolNameError(f"Tool contribution already registered: {registered.name}")
        self._contributions[registered.name] = registered
        return registered

    def unregister_plugin(self, plugin_id: str) -> None:
        """Remove all contributions owned by a plugin during rollback or shutdown."""
        names = [name for name, item in self._contributions.items() if item.plugin_id == plugin_id]
        for name in names:
            del self._contributions[name]

    def contributions(self) -> tuple[RegisteredToolContribution, ...]:
        """Return contributions ordered by their stable qualified name."""
        return tuple(self._contributions[name] for name in sorted(self._contributions))


class PluginToolRegistry:
    """Plugin-scoped write facade that cannot claim another plugin's namespace."""

    __slots__ = ("_plugin_id", "_registry")

    def __init__(self, plugin_id: str, registry: ToolRegistry) -> None:
        self._plugin_id = plugin_id
        self._registry = registry

    def register(self, contribution: ToolContribution) -> RegisteredToolContribution:
        """Register a tool under this context's plugin identifier."""
        return self._registry.register(self._plugin_id, contribution)
