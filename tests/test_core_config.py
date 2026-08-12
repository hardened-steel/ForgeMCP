from pathlib import Path

import pytest

from forgemcp.core.config import ForgeConfig
from forgemcp.core.errors import ConfigurationError, WorkspaceRootError


def test_config_is_created_from_explicit_environment(tmp_path):
    config = ForgeConfig.from_environment(
        {
            "FORGEMCP_WORKSPACE": str(tmp_path),
            "FORGEMCP_LOG_LEVEL": "debug",
            "FORGEMCP_EXTERNAL_PLUGINS_ENABLED": "true",
            "FORGEMCP_EXTERNAL_PLUGIN_ALLOWLIST": "cmake, clangd ",
        },
        cwd=Path("unused"),
    )

    assert config.workspace_root == tmp_path.resolve()
    assert config.log_level == "DEBUG"
    assert config.external_plugins_enabled is True
    assert config.external_plugin_allowlist == frozenset({"cmake", "clangd"})


def test_config_rejects_missing_workspace(tmp_path):
    with pytest.raises(WorkspaceRootError):
        ForgeConfig(workspace_root=tmp_path / "missing")


def test_config_rejects_unknown_log_level(tmp_path):
    with pytest.raises(ConfigurationError):
        ForgeConfig(workspace_root=tmp_path, log_level="verbose")


def test_config_rejects_ambiguous_external_plugin_enablement(tmp_path):
    with pytest.raises(ConfigurationError, match="FORGEMCP_EXTERNAL_PLUGINS_ENABLED"):
        ForgeConfig.from_environment(
            {
                "FORGEMCP_WORKSPACE": str(tmp_path),
                "FORGEMCP_EXTERNAL_PLUGINS_ENABLED": "sometimes",
            }
        )
