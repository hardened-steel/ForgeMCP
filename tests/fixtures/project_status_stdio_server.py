"""Test-only real-stdio composition for Project Intelligence failure/race gates."""

from __future__ import annotations

import asyncio
import os

from forgemcp.core import ForgeApplication, ForgeConfig
from forgemcp.debugger.models import DebuggerState
from forgemcp.plugins import ForgePlugin, PluginContext, PluginMetadata
from forgemcp.project import ComponentState, ComponentStatus, ProjectStatusRegistry, StatusFact
from forgemcp.project.models import utc_now
from forgemcp.server import create_server


class _FixtureProvider:
    def __init__(self, provider_id: str, mode: str) -> None:
        self.id = provider_id
        self.mode = mode
        self.calls = 0

    async def snapshot_status(self) -> ComponentStatus:
        self.calls += 1
        if self.mode == "failure":
            raise RuntimeError("password=stdio-secret")
        if self.mode in {"timeout", "shutdown"}:
            await asyncio.sleep(60)
        if self.mode == "concurrent":
            await asyncio.sleep(0.05)
        return ComponentStatus(
            id=self.id,
            display_name="MCP fixture provider",
            state=ComponentState.IDLE,
            summary="Safe test-only cached state.",
            facts=(StatusFact(name="snapshot_calls", value=self.calls),),
            observed_at=utc_now(),
        )


class _ProjectStatusFixturePlugin(ForgePlugin):
    def __init__(self, mode: str) -> None:
        super().__init__(
            PluginMetadata(
                plugin_id="zz_status_fixture",
                requires=("clangd", "cmake", "debugger", "quality"),
                requires_services=("project_status_registry", "plugins"),
            )
        )
        self._mode = mode
        self._registry: ProjectStatusRegistry | None = None
        self._provider_id: str | None = None

    async def start(self, context: PluginContext) -> None:
        registry = context.services.get("project_status_registry")
        if not isinstance(registry, ProjectStatusRegistry):
            raise TypeError("Fixture requires ProjectStatusRegistry.")
        self._registry = registry
        if self._mode in {"failure", "timeout", "shutdown", "concurrent"}:
            self._provider_id = f"fixture_{self._mode}"
            registry.register(_FixtureProvider(self._provider_id, self._mode))
        if self._mode == "active":
            manager = context.services.get("plugins")
            cmake = manager._records["cmake"].plugin.service
            debugger = manager._records["debugger"].plugin.service
            cmake._active_operations = 1
            async with debugger._state_lock:
                debugger._state = DebuggerState.PAUSED
        if self._mode == "configured_unavailable":
            manager = context.services.get("plugins")
            clangd = manager._records["clangd"].plugin.service
            clangd._availability_observed = True
            clangd._available = False

    async def stop(self) -> None:
        if self._registry is not None and self._provider_id is not None:
            self._registry.unregister(self._provider_id)
        self._registry = None
        self._provider_id = None


def main() -> None:
    mode = os.environ.get("FORGEMCP_PROJECT_STATUS_FIXTURE", "healthy")

    def application_factory() -> ForgeApplication:
        config = ForgeConfig.from_environment()
        return ForgeApplication.create(
            config,
            builtin_plugins=(_ProjectStatusFixturePlugin(mode),),
        )

    create_server(application_factory).run(transport="stdio")


if __name__ == "__main__":
    main()
