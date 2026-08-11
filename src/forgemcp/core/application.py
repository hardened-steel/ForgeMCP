"""Application composition root and lifecycle for ForgeMCP."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from forgemcp import __version__
from forgemcp.core.config import ForgeConfig
from forgemcp.core.errors import LifecycleError
from forgemcp.core.logging import StructuredLogger, create_logger
from forgemcp.core.services import ServiceRegistry


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

    @classmethod
    def create(cls, config: ForgeConfig) -> "ForgeApplication":
        """Compose Core's built-in services from already validated configuration."""
        services = ServiceRegistry()
        services.register("config", config)
        services.register("logger", create_logger(config.log_level))
        return cls(config, services)

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

    def start(self) -> None:
        """Enter the running state exactly once."""
        if self._state is not LifecycleState.CREATED:
            raise LifecycleError(f"Cannot start an application in state '{self._state}'.")
        self._state = LifecycleState.RUNNING
        self._logger.info("application_started", workspace_root=str(self.config.workspace_root))

    def stop(self) -> None:
        """Stop the application; repeated cleanup is harmless."""
        if self._state is LifecycleState.STOPPED:
            return
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
