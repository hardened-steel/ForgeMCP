"""Plugin contracts and lifecycle management for ForgeMCP providers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from forgemcp.config import ForgeConfig
from forgemcp.processes import ProcessManager
from forgemcp.workspace import WorkspaceService


@dataclass(frozen=True, slots=True)
class PluginContext:
    """Core services supplied to a plugin for one project session."""

    config: ForgeConfig
    workspace: WorkspaceService
    processes: ProcessManager


class ForgePlugin(Protocol):
    """Contract implemented by CMake, clangd, debugger, and future providers."""

    id: str
    requires: tuple[str, ...]
    capabilities: tuple[str, ...]

    async def start(self, context: PluginContext) -> None: ...

    async def stop(self) -> None: ...


class PluginRegistry:
    """Validates plugin dependencies and controls their deterministic lifecycle."""

    def __init__(self, plugins: Iterable[ForgePlugin] = ()) -> None:
        self._plugins: dict[str, ForgePlugin] = {}
        self._started: list[ForgePlugin] = []
        for plugin in plugins:
            self.add(plugin)

    def add(self, plugin: ForgePlugin) -> None:
        if plugin.id in self._plugins:
            raise ValueError(f"Duplicate plugin id: {plugin.id}")
        self._plugins[plugin.id] = plugin

    def get(self, plugin_id: str) -> ForgePlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError as error:
            raise KeyError(f"Plugin is not registered: {plugin_id}") from error

    def providers_for(self, capability: str) -> tuple[ForgePlugin, ...]:
        return tuple(
            plugin
            for plugin in self._plugins.values()
            if capability in plugin.capabilities
        )

    async def start_all(self, context: PluginContext) -> None:
        """Start plugins after starting every declared dependency."""
        remaining = dict(self._plugins)
        while remaining:
            ready = [
                plugin
                for plugin in remaining.values()
                if all(requirement in {item.id for item in self._started} for requirement in plugin.requires)
            ]
            if not ready:
                unresolved = ", ".join(sorted(remaining))
                raise ValueError(f"Unresolved or cyclic plugin dependencies: {unresolved}")
            for plugin in ready:
                await plugin.start(context)
                self._started.append(plugin)
                del remaining[plugin.id]

    async def stop_all(self) -> None:
        """Stop started plugins in reverse dependency order."""
        while self._started:
            await self._started.pop().stop()
