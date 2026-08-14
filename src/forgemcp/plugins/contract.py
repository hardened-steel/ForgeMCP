"""Transport-neutral public contracts for ForgeMCP feature plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from forgemcp.core.config import ForgeConfig
    from forgemcp.plugins.tools import PluginToolRegistry


PLUGIN_API_VERSION = "1"
"""The stable major API version implemented by this ForgeMCP release."""

_PLUGIN_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]*$")


def _normalise_identifiers(values: object, *, field_name: str) -> tuple[str, ...]:
    """Make identifier collections immutable while rejecting ambiguous duplicates."""
    if isinstance(values, str):
        raise ValueError(f"Plugin metadata '{field_name}' must be a collection of identifiers.")
    try:
        identifiers = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError(f"Plugin metadata '{field_name}' must be a collection of identifiers.") from error
    if any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
        raise ValueError(f"Plugin metadata '{field_name}' must contain non-empty strings.")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError(f"Plugin metadata '{field_name}' must not contain duplicates.")
    return identifiers


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """Immutable declaration used to validate and order a feature plugin."""

    plugin_id: str
    api_version: str = PLUGIN_API_VERSION
    requires: tuple[str, ...] = ()
    requires_services: tuple[str, ...] = ()
    provides: frozenset[str] = field(default_factory=frozenset)
    tool_namespaces: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.plugin_id, str) or not _PLUGIN_IDENTIFIER.fullmatch(self.plugin_id):
            raise ValueError(
                "Plugin metadata 'plugin_id' must be a lower-case identifier using letters, digits, hyphens, and underscores."
            )
        if not isinstance(self.api_version, str) or not self.api_version:
            raise ValueError("Plugin metadata 'api_version' must be a non-empty string.")
        requires = _normalise_identifiers(self.requires, field_name="requires")
        required_services = _normalise_identifiers(
            self.requires_services, field_name="requires_services"
        )
        capabilities = _normalise_identifiers(self.provides, field_name="provides")
        tool_namespaces = _normalise_identifiers(self.tool_namespaces, field_name="tool_namespaces")
        object.__setattr__(self, "requires", requires)
        object.__setattr__(self, "requires_services", required_services)
        object.__setattr__(self, "provides", frozenset(capabilities))
        object.__setattr__(self, "tool_namespaces", tool_namespaces)


class PluginLogger(Protocol):
    """The intentionally small logger surface available to a plugin."""

    def info(self, event: str, **context: Any) -> None:
        """Record a structured informational event."""

    def warning(self, event: str, **context: Any) -> None:
        """Record a structured warning event."""


class PluginServiceAccess:
    """Read-only, declaration-scoped view of Core services for one plugin."""

    __slots__ = ("_services", "_allowed_names")

    def __init__(self, services: object, allowed_names: tuple[str, ...]) -> None:
        self._services = services
        self._allowed_names = allowed_names

    def get(self, name: str) -> object:
        """Return a service explicitly declared in ``requires_services`` only."""
        if name not in self._allowed_names:
            raise KeyError(f"Plugin did not declare access to Core service: {name}")
        # The manager creates this object from ServiceRegistry after preflight.
        return self._services.get(name)  # type: ignore[union-attr]

    def names(self) -> tuple[str, ...]:
        """Return the stable names this plugin declared as dependencies."""
        return self._allowed_names


@dataclass(frozen=True, slots=True)
class PluginContext:
    """Capabilities supplied to ``ForgePlugin.start`` without an application reference."""

    config: ForgeConfig
    services: PluginServiceAccess
    logger: PluginLogger
    tools: PluginToolRegistry


class ForgePlugin(ABC):
    """Base class for a feature plugin with immutable declared metadata."""

    __slots__ = ("_metadata",)

    def __init__(self, metadata: PluginMetadata) -> None:
        self._metadata = metadata

    @property
    def metadata(self) -> PluginMetadata:
        """Return the immutable declaration fixed at plugin construction."""
        return self._metadata

    @abstractmethod
    async def start(self, context: PluginContext) -> None:
        """Start the plugin after all declared dependencies have started."""

    @abstractmethod
    async def stop(self) -> None:
        """Release plugin resources during reverse-order shutdown."""
