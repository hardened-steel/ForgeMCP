"""Conservative discovery for explicitly trusted external feature plugins."""

from __future__ import annotations

from importlib.metadata import EntryPoint, entry_points

from forgemcp.plugins.contract import ForgePlugin
from forgemcp.plugins.errors import PluginDiscoveryError

ENTRY_POINT_GROUP = "forgemcp.plugins"


def _group_entry_points() -> tuple[EntryPoint, ...]:
    """List entry-point metadata without importing any advertised plugin package."""
    discovered = entry_points()
    if hasattr(discovered, "select"):
        selected = discovered.select(group=ENTRY_POINT_GROUP)
    else:  # pragma: no cover - compatibility with older importlib.metadata providers
        selected = discovered.get(ENTRY_POINT_GROUP, ())
    return tuple(sorted(selected, key=lambda item: (item.name, item.value)))


def discover_allowed_plugins(
    *, enabled: bool, allowlist: frozenset[str]
) -> tuple[ForgePlugin, ...]:
    """Load only allow-listed entry points, and only when external code is enabled.

    Looking up distribution metadata is deferred until both policy gates pass.
    Calling ``EntryPoint.load`` imports arbitrary package code, so it is never
    called for an entry point whose *entry-point name* is not explicitly trusted.
    """
    if not enabled or not allowlist:
        return ()

    plugins: list[ForgePlugin] = []
    for candidate in _group_entry_points():
        if candidate.name not in allowlist:
            continue
        try:
            plugin = candidate.load()
        except Exception as error:
            raise PluginDiscoveryError(
                f"Could not load explicitly allowed plugin entry point: {candidate.name}"
            ) from error
        if not isinstance(plugin, ForgePlugin):
            raise PluginDiscoveryError(
                f"Plugin entry point '{candidate.name}' must resolve to a ForgePlugin instance."
            )
        if plugin.metadata.plugin_id != candidate.name:
            raise PluginDiscoveryError(
                "Plugin entry-point name must match its declared plugin_id: " f"{candidate.name}"
            )
        plugins.append(plugin)
    return tuple(plugins)
