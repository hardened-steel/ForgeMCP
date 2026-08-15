from pathlib import Path
import os

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
            "FORGEMCP_CONFIGURE_TIMEOUT_SEC": "12.5",
            "FORGEMCP_BUILD_TIMEOUT_SEC": "34",
            "FORGEMCP_TEST_TIMEOUT_SEC": "56",
            "FORGEMCP_CLANGD": str(tmp_path / "tools" / "clangd.exe"),
            "FORGEMCP_LLDB_DAP": str(tmp_path / "tools" / "lldb-dap.exe"),
            "FORGEMCP_CLANG_FORMAT": str(tmp_path / "tools" / "clang-format.exe"),
            "FORGEMCP_CLANG_TIDY": str(tmp_path / "tools" / "clang-tidy.exe"),
            "FORGEMCP_COMPILE_COMMANDS": "required",
        },
        cwd=Path("unused"),
    )

    assert config.workspace_root == tmp_path.resolve()
    assert config.log_level == "DEBUG"
    assert config.external_plugins_enabled is True
    assert config.external_plugin_allowlist == frozenset({"cmake", "clangd"})
    assert config.configure_timeout_seconds == 12.5
    assert config.build_timeout_seconds == 34.0
    assert config.test_timeout_seconds == 56.0
    assert config.clangd_path == (tmp_path / "tools" / "clangd.exe").resolve(strict=False)
    assert config.lldb_dap_path == (tmp_path / "tools" / "lldb-dap.exe").absolute()
    assert config.clang_format_path == (tmp_path / "tools" / "clang-format.exe").absolute()
    assert config.clang_tidy_path == (tmp_path / "tools" / "clang-tidy.exe").absolute()
    assert config.compile_commands == "required"


@pytest.mark.parametrize("value", ["invalid", "ON"])
def test_config_rejects_invalid_compile_commands_mode(tmp_path, value):
    with pytest.raises(ConfigurationError):
        ForgeConfig.from_environment({"FORGEMCP_WORKSPACE": str(tmp_path), "FORGEMCP_COMPILE_COMMANDS": value})


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


@pytest.mark.parametrize("variable, value", [
    ("FORGEMCP_CONFIGURE_TIMEOUT_SEC", "zero"),
    ("FORGEMCP_BUILD_TIMEOUT_SEC", "0"),
    ("FORGEMCP_TEST_TIMEOUT_SEC", "3601"),
])
def test_environment_timeout_values_are_parsed_and_bounded(tmp_path, variable, value):
    with pytest.raises(ConfigurationError):
        ForgeConfig.from_environment({"FORGEMCP_WORKSPACE": str(tmp_path), variable: value})


def test_generator_and_preset_are_an_explicit_configuration_conflict(tmp_path):
    with pytest.raises(ConfigurationError, match="cannot both"):
        ForgeConfig.from_environment({
            "FORGEMCP_WORKSPACE": str(tmp_path),
            "FORGEMCP_CMAKE_GENERATOR": "Ninja",
            "FORGEMCP_CONFIGURE_PRESET": "dev",
        })


@pytest.mark.skipif(os.name != "nt", reason="Windows path namespace policy")
@pytest.mark.parametrize("value", [r"\\server\share\cmake.exe", r"\\?\C:\\tool\cmake.exe"])
def test_config_rejects_unc_and_device_executable_paths(tmp_path, value):
    with pytest.raises(ConfigurationError, match="absolute executable path"):
        ForgeConfig.from_environment({
            "FORGEMCP_WORKSPACE": str(tmp_path),
            "FORGEMCP_CMAKE": value,
        })


def test_config_rejects_relative_lldb_dap_path(tmp_path):
    with pytest.raises(ConfigurationError, match="FORGEMCP_LLDB_DAP"):
        ForgeConfig(workspace_root=tmp_path, lldb_dap_path=Path("lldb-dap"))


@pytest.mark.parametrize("field, value", [("clang_format_path", Path("clang-format")), ("clang_tidy_path", Path("clang-tidy"))])
def test_config_rejects_relative_quality_tool_paths(tmp_path, field, value):
    with pytest.raises(ConfigurationError):
        ForgeConfig(workspace_root=tmp_path, **{field: value})
