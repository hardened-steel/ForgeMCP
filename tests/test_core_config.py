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
            "FORGEMCP_CLANGD": str(tmp_path / "tools" / "clangd.exe"),
            "FORGEMCP_LLDB_DAP": str(tmp_path / "tools" / "lldb-dap.exe"),
        },
        cwd=Path("unused"),
    )

    assert config.workspace_root == tmp_path.resolve()
    assert config.log_level == "DEBUG"
    assert config.external_plugins_enabled is True
    assert config.external_plugin_allowlist == frozenset({"cmake", "clangd"})
    assert config.clangd_path == (tmp_path / "tools" / "clangd.exe").resolve(strict=False)
    assert config.lldb_dap_path == (tmp_path / "tools" / "lldb-dap.exe").absolute()


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


def test_config_rejects_relative_clangd_path(tmp_path):
    with pytest.raises(ConfigurationError, match="FORGEMCP_CLANGD"):
        ForgeConfig(workspace_root=tmp_path, clangd_path=Path("clangd"))


def test_config_rejects_relative_lldb_dap_path(tmp_path):
    with pytest.raises(ConfigurationError, match="FORGEMCP_LLDB_DAP"):
        ForgeConfig(workspace_root=tmp_path, lldb_dap_path=Path("lldb-dap"))
