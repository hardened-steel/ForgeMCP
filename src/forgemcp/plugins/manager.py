"""Dependency resolution, lifecycle, and discovery orchestration for plugins."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from forgemcp.plugins.contract import (
    PLUGIN_API_VERSION,
    ForgePlugin,
    PluginContext,
    PluginServiceAccess,
)
from forgemcp.plugins.discovery import discover_allowed_plugins
from forgemcp.plugins.errors import (
    DuplicateCapabilityError,
    DuplicatePluginIdError,
    MissingPluginDependencyError,
    MissingRequiredServiceError,
    PluginApiVersionError,
    PluginDependencyCycleError,
    PluginManagerClosedError,
    PluginRegistrationError,
    PluginStartError,
)
from forgemcp.plugins.tools import PluginToolRegistry, ToolRegistry
from forgemcp.plugins.surface import (
    DiscoverySurfaceRegistry,
    PluginCompletionRegistry,
    PluginPromptRegistry,
    PluginResourceRegistry,
    PluginResourceTemplateRegistry,
)

class PluginState(StrEnum):
    """Observable lifecycle state for one registered plugin."""

    REGISTERED = "registered"
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PluginStatus:
    """Structured, safe status information for a feature plugin."""

    plugin_id: str
    state: PluginState
    provides: tuple[str, ...]
    source: str
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return a serialization-friendly diagnostic representation."""
        return {
            "plugin_id": self.plugin_id,
            "state": self.state.value,
            "provides": list(self.provides),
            "source": self.source,
            "error": self.error,
        }


@dataclass(slots=True)
class _PluginRecord:
    plugin: ForgePlugin
    source: str
    state: PluginState = PluginState.REGISTERED
    error: str | None = None


class PluginManager:
    """Own feature-plugin validation and lifecycle without exposing the application."""

    def __init__(self, *, config: object, services: object, logger: object) -> None:
        self._config = config
        self._services = services
        self._logger = logger
        self._records: dict[str, _PluginRecord] = {}
        self._capability_owners: dict[str, str] = {}
        self._tools = ToolRegistry()
        self._surface = DiscoverySurfaceRegistry()
        self._started_ids: list[str] = []
        self._external_discovery_done = False
        self._closed = False

    @property
    def tools(self) -> ToolRegistry:
        """Return the application-owned, transport-neutral tool registry."""
        return self._tools

    @property
    def surface(self) -> DiscoverySurfaceRegistry:
        """Return the application-owned non-tool contribution registry."""
        return self._surface

    def register_builtin(self, plugin: ForgePlugin) -> None:
        """Explicitly register a trusted plugin composed by ForgeMCP itself."""
        self._register(plugin, source="builtin")

    def register_external(self, plugin: ForgePlugin) -> None:
        """Register an external plugin that passed the discovery trust gate."""
        self._register(plugin, source="external")

    def _register(self, plugin: ForgePlugin, *, source: str) -> None:
        if self._closed:
            raise PluginManagerClosedError("Cannot register plugins after the manager has closed.")
        if not isinstance(plugin, ForgePlugin):
            raise PluginRegistrationError("Plugins must inherit ForgePlugin.")
        metadata = plugin.metadata
        if metadata.api_version != PLUGIN_API_VERSION:
            raise PluginApiVersionError(
                f"Plugin '{metadata.plugin_id}' requires API version {metadata.api_version}; "
                f"this server implements {PLUGIN_API_VERSION}."
            )
        if metadata.plugin_id in self._records:
            raise DuplicatePluginIdError(f"Plugin already registered: {metadata.plugin_id}")
        for capability in metadata.provides:
            owner = self._capability_owners.get(capability)
            if owner is not None:
                raise DuplicateCapabilityError(
                    f"Capability '{capability}' is already provided by plugin '{owner}'."
                )
        self._records[metadata.plugin_id] = _PluginRecord(plugin=plugin, source=source)
        for capability in metadata.provides:
            self._capability_owners[capability] = metadata.plugin_id

    def discover_external_plugins(self) -> tuple[str, ...]:
        """Load configured external plugins once, using the conservative policy gates."""
        if self._external_discovery_done:
            return ()
        self._external_discovery_done = True
        enabled = bool(getattr(self._config, "external_plugins_enabled", False))
        configured_allowlist = getattr(self._config, "external_plugin_allowlist", frozenset())
        allowlist = frozenset(configured_allowlist)
        plugins = discover_allowed_plugins(enabled=enabled, allowlist=allowlist)
        for plugin in plugins:
            self.register_external(plugin)
        return tuple(plugin.metadata.plugin_id for plugin in plugins)

    def statuses(self) -> tuple[PluginStatus, ...]:
        """Return every plugin's status in stable identifier order."""
        return tuple(
            PluginStatus(
                plugin_id=plugin_id,
                state=record.state,
                provides=tuple(sorted(record.plugin.metadata.provides)),
                source=record.source,
                error=record.error,
            )
            for plugin_id, record in sorted(self._records.items())
        )

    async def start(self) -> None:
        """Discover, validate, and start plugins in deterministic topological order."""
        if self._closed:
            raise PluginManagerClosedError("Cannot start a plugin manager after it has closed.")
        if self._started_ids:
            return
        self.discover_external_plugins()
        start_order = self._resolve_start_order()
        for plugin_id in start_order:
            record = self._records[plugin_id]
            record.state = PluginState.STARTING
            context = PluginContext(
                config=self._config,
                services=PluginServiceAccess(
                    self._services, record.plugin.metadata.requires_services
                ),
                logger=self._logger,
                tools=PluginToolRegistry(
                    plugin_id, self._tools, record.plugin.metadata.tool_namespaces
                ),
                resources=PluginResourceRegistry(plugin_id, self._surface),
                resource_templates=PluginResourceTemplateRegistry(plugin_id, self._surface),
                prompts=PluginPromptRegistry(plugin_id, self._surface),
                completions=PluginCompletionRegistry(plugin_id, self._surface),
            )
            try:
                await record.plugin.start(context)
            except Exception as error:
                record.state = PluginState.FAILED
                record.error = type(error).__name__
                self._tools.unregister_plugin(plugin_id)
                self._surface.unregister_plugin(plugin_id)
                try:
                    # start() may already have registered status providers or
                    # acquired other application-scoped resources. Give the
                    # failing plugin the same idempotent partial-start cleanup
                    # opportunity before rolling back its dependencies.
                    await record.plugin.stop()
                except Exception as cleanup_error:  # pragma: no cover - defensive cleanup path
                    self._logger.warning(
                        "plugin_failed_start_cleanup_failed",
                        plugin_id=plugin_id,
                        failure_category=type(cleanup_error).__name__,
                    )
                await self._rollback_started()
                await self._surface.aclose()
                self._closed = True
                raise PluginStartError(f"Plugin failed to start: {plugin_id}") from error
            record.state = PluginState.RUNNING
            self._started_ids.append(plugin_id)

    async def aclose(self) -> None:
        """Stop every successfully started plugin in reverse start order exactly once."""
        if self._closed:
            return
        self._closed = True
        await self._rollback_started()
        await self._surface.aclose()

    def _resolve_start_order(self) -> tuple[str, ...]:
        """Validate graph/service dependencies and return a stable Kahn ordering."""
        dependency_sets: dict[str, set[str]] = {}
        for plugin_id, record in self._records.items():
            metadata = record.plugin.metadata
            missing_plugins = sorted(set(metadata.requires).difference(self._records))
            if missing_plugins:
                raise MissingPluginDependencyError(
                    f"Plugin '{plugin_id}' requires unregistered plugins: {', '.join(missing_plugins)}"
                )
            missing_services = sorted(
                service for service in metadata.requires_services if service not in self._services
            )
            if missing_services:
                raise MissingRequiredServiceError(
                    f"Plugin '{plugin_id}' requires unavailable Core services: "
                    f"{', '.join(missing_services)}"
                )
            dependency_sets[plugin_id] = set(metadata.requires)

        order: list[str] = []
        ready = sorted(plugin_id for plugin_id, dependencies in dependency_sets.items() if not dependencies)
        while ready:
            current = ready.pop(0)
            order.append(current)
            for candidate in sorted(dependency_sets):
                dependencies = dependency_sets[candidate]
                if current not in dependencies:
                    continue
                dependencies.remove(current)
                if not dependencies:
                    ready.append(candidate)
                    ready.sort()
        if len(order) != len(dependency_sets):
            cycle_members = sorted(set(dependency_sets).difference(order))
            raise PluginDependencyCycleError(
                f"Plugin dependency cycle detected: {', '.join(cycle_members)}"
            )
        return tuple(order)

    async def _rollback_started(self) -> None:
        """Best-effort reverse cleanup for startup failure and normal shutdown."""
        while self._started_ids:
            plugin_id = self._started_ids.pop()
            record = self._records[plugin_id]
            try:
                await record.plugin.stop()
            except Exception as error:  # pragma: no cover - defensive cleanup path
                record.state = PluginState.FAILED
                record.error = type(error).__name__
                self._logger.warning(
                    "plugin_stop_failed",
                    plugin_id=plugin_id,
                    failure_category=type(error).__name__,
                )
            else:
                record.state = PluginState.STOPPED
                record.error = None
            finally:
                self._tools.unregister_plugin(plugin_id)
                self._surface.unregister_plugin(plugin_id)
