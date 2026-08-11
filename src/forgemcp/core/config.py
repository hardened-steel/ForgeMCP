"""Explicit, validated configuration for a ForgeMCP application instance."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from forgemcp.core.errors import ConfigurationError, WorkspaceRootError


@dataclass(frozen=True, slots=True)
class ForgeConfig:
    """Immutable Core configuration created before the application starts."""

    workspace_root: Path
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        root = self.workspace_root.resolve()
        if not root.exists() or not root.is_dir():
            raise WorkspaceRootError("FORGEMCP_WORKSPACE must name an existing directory.")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("FORGEMCP_LOG_LEVEL must be a standard logging level.")
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "log_level", self.log_level.upper())

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
        *,
        cwd: Path | None = None,
    ) -> "ForgeConfig":
        """Create configuration from an environment mapping at application creation time."""
        values = os.environ if environment is None else environment
        current_directory = Path.cwd() if cwd is None else cwd
        workspace = Path(values.get("FORGEMCP_WORKSPACE", str(current_directory)))
        return cls(
            workspace_root=workspace,
            log_level=values.get("FORGEMCP_LOG_LEVEL", "INFO"),
        )
