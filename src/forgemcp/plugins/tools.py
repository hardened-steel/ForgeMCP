"""Transport-neutral declarations and registry for feature-provided tools."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from forgemcp.plugins.errors import DuplicateToolNameError, ToolNamespaceError
from forgemcp.plugins.execution import ToolExecutionContext

_TOOL_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_PLUGIN_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]*$")

LegacyToolHandler = Callable[[Mapping[str, object]], object | Awaitable[object]]
"""Mapping-only handler retained for external plugin compatibility."""

ContextAwareToolHandler = Callable[[Mapping[str, object], ToolExecutionContext], object | Awaitable[object]]
"""Handler shape for tools that opt into the request-scoped execution context."""

ToolHandler = LegacyToolHandler | ContextAwareToolHandler


def handler_accepts_execution_context(handler: ToolHandler) -> bool:
    """Return whether a handler explicitly opts into the v1 context keyword.

    Only the named keyword is considered an opt-in.  This avoids accidentally
    binding context into a legacy handler's optional positional/default values.
    """
    try:
        parameters = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        return False
    parameter = parameters.get("execution_context") or parameters.get("context")
    return parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


def invoke_tool_handler(
    handler: ToolHandler,
    arguments: Mapping[str, object],
    execution_context: ToolExecutionContext,
) -> object | Awaitable[object]:
    """Invoke old or context-aware contribution handlers without SDK leakage."""
    if handler_accepts_execution_context(handler):
        try:
            parameters = inspect.signature(handler).parameters
        except (TypeError, ValueError):  # pragma: no cover - guarded above
            return handler(arguments)
        name = "execution_context" if "execution_context" in parameters else "context"
        return handler(arguments, **{name: execution_context})  # type: ignore[call-arg]
    return handler(arguments)


@dataclass(frozen=True, slots=True)
class ToolContribution:
    """One operation offered by a plugin, independent of an MCP SDK."""

    name: str
    description: str
    handler: ToolHandler = field(repr=False, compare=False)
    input_model: type[BaseModel] | None = field(default=None, repr=False, compare=False)
    namespace: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _TOOL_IDENTIFIER.fullmatch(self.name):
            raise ToolNamespaceError(
                "Tool contribution names must be lower-case identifiers using letters, digits, and underscores."
            )
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("Tool contribution descriptions must not be empty.")
        if self.input_model is not None and (
            not isinstance(self.input_model, type) or not issubclass(self.input_model, BaseModel)
        ):
            raise TypeError("Tool contribution input_model must be a Pydantic model class when supplied.")
        if self.namespace is not None and (
            not isinstance(self.namespace, str) or not _PLUGIN_IDENTIFIER.fullmatch(self.namespace)
        ):
            raise ToolNamespaceError("Tool contribution namespaces must be lower-case plugin-style identifiers.")


@dataclass(frozen=True, slots=True)
class RegisteredToolContribution:
    """A contribution qualified with its owning plugin's stable namespace."""

    plugin_id: str
    contribution: ToolContribution

    @property
    def namespace(self) -> str:
        """Return the fixed public tool namespace selected by the contribution."""
        return self.contribution.namespace or self.plugin_id

    @property
    def name(self) -> str:
        """Return the MCP-safe stable qualified name, e.g. ``cmake__configure``."""
        return f"{self.namespace}__{self.contribution.name}"

    @property
    def description(self) -> str:
        """Return the transport-neutral contribution description."""
        return self.contribution.description

    @property
    def handler(self) -> ToolHandler:
        """Return the contribution handler without exposing a transport object."""
        return self.contribution.handler

    @property
    def input_model(self) -> type[BaseModel] | None:
        """Return the optional transport-neutral Pydantic input contract."""
        return self.contribution.input_model


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

    __slots__ = ("_plugin_id", "_registry", "_allowed_namespaces")

    def __init__(
        self, plugin_id: str, registry: ToolRegistry, extra_namespaces: tuple[str, ...] = ()
    ) -> None:
        self._plugin_id = plugin_id
        self._registry = registry
        self._allowed_namespaces = frozenset((plugin_id, *extra_namespaces))

    def register(self, contribution: ToolContribution) -> RegisteredToolContribution:
        """Register a tool under this plugin's declared public namespace set."""
        if contribution.namespace is not None and contribution.namespace not in self._allowed_namespaces:
            raise ToolNamespaceError("Tool contribution namespace was not declared by its plugin metadata.")
        return self._registry.register(self._plugin_id, contribution)
