"""Application composition root and lifecycle for ForgeMCP."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from forgemcp import __version__
from forgemcp.cmake import CMakePlugin
from forgemcp.clangd import ClangdPlugin
from forgemcp.debugger import DebuggerPlugin
from forgemcp.quality import QualityPlugin
from forgemcp.core.config import ForgeConfig
from forgemcp.core.errors import LifecycleError
from forgemcp.core.logging import StructuredLogger, create_logger
from forgemcp.core.services import ServiceRegistry
from forgemcp.processes import ProcessRuntime
from forgemcp.plugins import ForgePlugin, PluginManager
from forgemcp.project import (
    CoreStatusProvider,
    PluginManagerStatusProvider,
    ProcessRuntimeStatusProvider,
    ProjectPlugin,
    ProjectStatusRegistry,
    ProjectStatusService,
    WorkspaceStatusProvider,
)
from forgemcp.workspace import WorkspaceService


class LifecycleState(StrEnum):
    """The valid lifecycle states of a ForgeMCP application."""

    CREATED = "created"
    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class ServerStatus:
    """Safe diagnostic data returned by the Core status tool."""

    version: str
    workspace_root: str
    state: LifecycleState
    services: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "workspace_root": self.workspace_root,
            "state": self.state.value,
            "services": list(self.services),
        }


class ForgeApplication:
    """Owns immutable configuration, Core services, and application lifecycle."""

    def __init__(self, config: ForgeConfig, services: ServiceRegistry) -> None:
        self.config = config
        self.services = services
        self._state = LifecycleState.CREATED
        self._logger = services.get("logger")
        if not isinstance(self._logger, StructuredLogger):
            raise TypeError("The 'logger' service must be a StructuredLogger.")
        if not isinstance(services.get("plugins"), PluginManager):
            raise TypeError("The 'plugins' service must be a PluginManager.")

    @classmethod
    def create(
        cls, config: ForgeConfig, *, builtin_plugins: Iterable[ForgePlugin] = ()
    ) -> "ForgeApplication":
        """Compose Core services and registered domain services from validated configuration."""
        services = ServiceRegistry()
        services.register("config", config)
        logger = create_logger(config.log_level)
        services.register("logger", logger)
        workspace = WorkspaceService(config, logger)
        process_runtime = ProcessRuntime(config, logger)
        services.register("workspace", workspace)
        services.register("process_runtime", process_runtime)
        project_status_registry = ProjectStatusRegistry()
        services.register("project_status_registry", project_status_registry)
        services.register(
            "project_status_service",
            ProjectStatusService(project_status_registry, config.workspace_root),
        )
        plugins = PluginManager(config=config, services=services, logger=logger)
        services.register("plugins", plugins)
        for plugin in (
            CMakePlugin(), ClangdPlugin(), DebuggerPlugin(), ProjectPlugin(), QualityPlugin(),
            *tuple(builtin_plugins),
        ):
            plugins.register_builtin(plugin)
        application = cls(config, services)
        project_status_registry.register(CoreStatusProvider(lambda: application.state.value))
        project_status_registry.register(WorkspaceStatusProvider(workspace))
        project_status_registry.register(ProcessRuntimeStatusProvider(process_runtime))
        project_status_registry.register(
            PluginManagerStatusProvider(
                plugins, external_enabled=config.external_plugins_enabled
            )
        )
        return application

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        cwd: Path | None = None,
    ) -> "ForgeApplication":
        """Create an application once from process configuration."""
        return cls.create(ForgeConfig.from_environment(environment, cwd=cwd))

    @property
    def state(self) -> LifecycleState:
        return self._state

    async def start(self) -> None:
        """Start feature plugins and enter the running state exactly once."""
        if self._state is not LifecycleState.CREATED:
            raise LifecycleError(f"Cannot start an application in state '{self._state}'.")
        plugins = self.services.get("plugins")
        if not isinstance(plugins, PluginManager):
            raise TypeError("The 'plugins' service must be a PluginManager.")
        await plugins.start()
        self._state = LifecycleState.RUNNING
        self._logger.info("application_started", workspace_root=str(self.config.workspace_root))

    def stop(self) -> None:
        """Synchronously stop when no event loop is active; async hosts use ``aclose``."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        raise LifecycleError("Use 'await application.aclose()' from an active event loop.")

    async def aclose(self) -> None:
        """Await shutdown of async domain services before marking the app stopped.

        Plugin cleanup runs before the Process Runtime, so protocol adapters
        can release their handles before the runtime's final process cleanup.
        Async hosts, including the stdio server, must use this method.
        """
        if self._state is LifecycleState.STOPPED:
            return
        try:
            if "project_status_registry" in self.services:
                project_registry = self.services.get("project_status_registry")
                if isinstance(project_registry, ProjectStatusRegistry):
                    await project_registry.aclose()
            plugins = self.services.get("plugins")
            if not isinstance(plugins, PluginManager):
                raise TypeError("The 'plugins' service must be a PluginManager.")
            await plugins.aclose()
        finally:
            try:
                process_runtime = self.services.get("process_runtime")
                if isinstance(process_runtime, ProcessRuntime):
                    await process_runtime.aclose()
            finally:
                self._state = LifecycleState.STOPPED
                self._logger.info("application_stopped")

    def status(self) -> ServerStatus:
        """Return safe diagnostic state without inspecting project contents."""
        return ServerStatus(
            version=__version__,
            workspace_root=str(self.config.workspace_root),
            state=self.state,
            services=self.services.names(),
        )
