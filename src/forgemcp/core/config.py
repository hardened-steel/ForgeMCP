"""Explicit, validated configuration for a ForgeMCP application instance."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from forgemcp.core.errors import ConfigurationError, WorkspaceRootError


@dataclass(frozen=True, slots=True)
class ForgeConfig:
    """Immutable Core configuration created before the application starts."""

    workspace_root: Path
    log_level: str = "INFO"
    external_plugins_enabled: bool = False
    external_plugin_allowlist: frozenset[str] = field(default_factory=frozenset)
    clangd_path: Path | None = None
    lldb_dap_path: Path | None = None

    def __post_init__(self) -> None:
        root = self.workspace_root.resolve()
        if not root.exists() or not root.is_dir():
            raise WorkspaceRootError("FORGEMCP_WORKSPACE must name an existing directory.")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("FORGEMCP_LOG_LEVEL must be a standard logging level.")
        if not isinstance(self.external_plugins_enabled, bool):
            raise ConfigurationError("External plugin discovery must be configured as a boolean.")
        if isinstance(self.external_plugin_allowlist, str):
            raise ConfigurationError("External plugin allow-list must be a collection of entry-point names.")
        try:
            allowlist = frozenset(self.external_plugin_allowlist)
        except TypeError as error:
            raise ConfigurationError(
                "External plugin allow-list must be a collection of entry-point names."
            ) from error
        if any(not isinstance(name, str) or not name for name in allowlist):
            raise ConfigurationError("External plugin allow-list entries must be non-empty strings.")
        clangd_path = self.clangd_path
        if clangd_path is not None:
            if not isinstance(clangd_path, Path):
                raise ConfigurationError("FORGEMCP_CLANGD must be an absolute executable path.")
            if not clangd_path.is_absolute():
                raise ConfigurationError("FORGEMCP_CLANGD must be an absolute executable path.")
            clangd_path = clangd_path.resolve(strict=False)
        lldb_dap_path = self.lldb_dap_path
        if lldb_dap_path is not None:
            if not isinstance(lldb_dap_path, Path):
                raise ConfigurationError("FORGEMCP_LLDB_DAP must be an absolute executable path.")
            if not lldb_dap_path.is_absolute():
                raise ConfigurationError("FORGEMCP_LLDB_DAP must be an absolute executable path.")
            # Preserve the configured lexical path so ProcessPolicy can
            # explicitly reject a symlink/reparse-point adapter at approval.
            lldb_dap_path = lldb_dap_path.absolute()
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "log_level", self.log_level.upper())
        object.__setattr__(self, "external_plugin_allowlist", allowlist)
        object.__setattr__(self, "clangd_path", clangd_path)
        object.__setattr__(self, "lldb_dap_path", lldb_dap_path)

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
        external_plugins_enabled = _read_boolean(
            values.get("FORGEMCP_EXTERNAL_PLUGINS_ENABLED", "false"),
            variable_name="FORGEMCP_EXTERNAL_PLUGINS_ENABLED",
        )
        external_plugin_allowlist = frozenset(
            name.strip()
            for name in values.get("FORGEMCP_EXTERNAL_PLUGIN_ALLOWLIST", "").split(",")
            if name.strip()
        )
        raw_clangd_path = values.get("FORGEMCP_CLANGD", "").strip()
        raw_lldb_dap_path = values.get("FORGEMCP_LLDB_DAP", "").strip()
        return cls(
            workspace_root=workspace,
            log_level=values.get("FORGEMCP_LOG_LEVEL", "INFO"),
            external_plugins_enabled=external_plugins_enabled,
            external_plugin_allowlist=external_plugin_allowlist,
            clangd_path=Path(raw_clangd_path) if raw_clangd_path else None,
            lldb_dap_path=Path(raw_lldb_dap_path) if raw_lldb_dap_path else None,
        )


def _read_boolean(value: str, *, variable_name: str) -> bool:
    """Read an unambiguous environment boolean without enabling code by accident."""
    normalised = value.strip().lower()
    if normalised in {"1", "true", "yes"}:
        return True
    if normalised in {"0", "false", "no", ""}:
        return False
    raise ConfigurationError(f"{variable_name} must be one of true/false, yes/no, or 1/0.")
