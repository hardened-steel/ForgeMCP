"""Immutable, source-aware configuration assembled only at the composition boundary.

Environment variables are deliberately read here (or passed in by a CLI host) and
nowhere in feature code. The immutable config retains a private host-environment
snapshot for safe tool discovery, but exposes only source metadata to diagnostics.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any

from forgemcp.core.errors import ConfigurationError, WorkspaceRootError


class ConfigurationSource(StrEnum):
    """Safe origin category for an effective setting."""

    CLI = "cli"
    ENVIRONMENT = "environment"
    DISCOVERY = "discovery"
    DEFAULT = "default"


_PATH_FIELDS = frozenset(
    {
        "cmake_path", "ctest_path", "clangd_path", "clang_format_path",
        "clang_tidy_path", "lldb_dap_path",
    }
)
_RELATIVE_DIRECTORY_FIELDS = frozenset({"cmake_source_dir", "build_dir"})
_SOURCE_DEFAULTS = {
    "workspace_root": ConfigurationSource.DEFAULT,
    "log_level": ConfigurationSource.DEFAULT,
    "external_plugins_enabled": ConfigurationSource.DEFAULT,
    "external_plugin_allowlist": ConfigurationSource.DEFAULT,
    "cmake_source_dir": ConfigurationSource.DEFAULT,
    "build_dir": ConfigurationSource.DEFAULT,
    "cmake_path": ConfigurationSource.DISCOVERY,
    "ctest_path": ConfigurationSource.DISCOVERY,
    "clangd_path": ConfigurationSource.DISCOVERY,
    "clang_format_path": ConfigurationSource.DISCOVERY,
    "clang_tidy_path": ConfigurationSource.DISCOVERY,
    "lldb_dap_path": ConfigurationSource.DISCOVERY,
    "toolchain": ConfigurationSource.DEFAULT,
    "host_arch": ConfigurationSource.DEFAULT,
    "target_arch": ConfigurationSource.DEFAULT,
    "visual_studio_instance": ConfigurationSource.DEFAULT,
    "cmake_generator": ConfigurationSource.DEFAULT,
    "configure_preset": ConfigurationSource.DEFAULT,
    "default_configuration": ConfigurationSource.DEFAULT,
    "configure_timeout_seconds": ConfigurationSource.DEFAULT,
    "build_timeout_seconds": ConfigurationSource.DEFAULT,
    "test_timeout_seconds": ConfigurationSource.DEFAULT,
}


@dataclass(frozen=True, slots=True)
class ForgeConfig:
    """Validated configuration for exactly one ForgeMCP application instance."""

    workspace_root: Path
    log_level: str = "INFO"
    external_plugins_enabled: bool = False
    external_plugin_allowlist: frozenset[str] = field(default_factory=frozenset)
    cmake_source_dir: str = "."
    build_dir: str | None = None
    cmake_path: Path | None = None
    ctest_path: Path | None = None
    clangd_path: Path | None = None
    clang_format_path: Path | None = None
    clang_tidy_path: Path | None = None
    lldb_dap_path: Path | None = None
    toolchain: str = "auto"
    host_arch: str = "auto"
    target_arch: str = "auto"
    visual_studio_instance: str | None = None
    cmake_generator: str | None = None
    configure_preset: str | None = None
    default_configuration: str | None = None
    configure_timeout_seconds: float = 300.0
    build_timeout_seconds: float = 900.0
    test_timeout_seconds: float = 900.0
    _sources: Mapping[str, ConfigurationSource] = field(default_factory=dict, repr=False, compare=False)
    _host_environment: Mapping[str, str] = field(default_factory=lambda: dict(os.environ), repr=False, compare=False)

    def __post_init__(self) -> None:
        root = self.workspace_root.resolve()
        if not root.exists() or not root.is_dir():
            raise WorkspaceRootError("FORGEMCP_WORKSPACE must name an existing directory.")
        level = self.log_level.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("FORGEMCP_LOG_LEVEL must be a standard logging level.")
        if not isinstance(self.external_plugins_enabled, bool):
            raise ConfigurationError("External plugin discovery must be configured as a boolean.")
        if isinstance(self.external_plugin_allowlist, str):
            raise ConfigurationError("External plugin allow-list must be a collection of entry-point names.")
        try:
            allowlist = frozenset(self.external_plugin_allowlist)
        except TypeError as error:
            raise ConfigurationError("External plugin allow-list must be a collection of entry-point names.") from error
        if any(not isinstance(name, str) or not name for name in allowlist):
            raise ConfigurationError("External plugin allow-list entries must be non-empty strings.")
        for field_name in _RELATIVE_DIRECTORY_FIELDS:
            value = getattr(self, field_name)
            if value is None and field_name == "build_dir":
                continue
            object.__setattr__(self, field_name, _normalise_relative_directory(value, field_name))
        for field_name in _PATH_FIELDS:
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _normalise_tool_path(value, field_name))
        if self.toolchain not in {"auto", "msvc", "llvm"}:
            raise ConfigurationError("toolchain must be auto, msvc, or llvm.")
        for field_name in ("host_arch", "target_arch"):
            if getattr(self, field_name) not in {"auto", "x64", "x86", "arm64"}:
                raise ConfigurationError(f"{field_name} must be auto, x64, x86, or arm64.")
        _validate_short_text(self.visual_studio_instance, "visual_studio_instance")
        _validate_short_text(self.cmake_generator, "cmake_generator")
        _validate_short_text(self.configure_preset, "configure_preset")
        _validate_short_text(self.default_configuration, "default_configuration")
        for field_name in ("configure_timeout_seconds", "build_timeout_seconds", "test_timeout_seconds"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 3600:
                raise ConfigurationError(f"{field_name} must be between 0 and 3600 seconds.")
            object.__setattr__(self, field_name, float(value))
        sources = dict(_SOURCE_DEFAULTS)
        for name, source in dict(self._sources).items():
            if name not in _SOURCE_DEFAULTS:
                raise ConfigurationError("Configuration source metadata contains an unknown setting.")
            try:
                sources[name] = ConfigurationSource(source)
            except ValueError as error:
                raise ConfigurationError("Configuration source metadata is invalid.") from error
        object.__setattr__(self, "workspace_root", root)
        object.__setattr__(self, "log_level", level)
        object.__setattr__(self, "external_plugin_allowlist", allowlist)
        object.__setattr__(self, "_sources", MappingProxyType(sources))
        object.__setattr__(self, "_host_environment", MappingProxyType(_validated_environment(self._host_environment)))

    @property
    def host_environment(self) -> Mapping[str, str]:
        """Read-only composition snapshot; callers must never log or expose it."""
        return self._host_environment

    def source_of(self, setting: str) -> ConfigurationSource:
        """Return safe provenance without returning the supplied raw value."""
        try:
            return self._sources[setting]
        except KeyError as error:
            raise KeyError(f"Unknown ForgeConfig setting: {setting}") from error

    def sanitized_effective_config(self) -> dict[str, object]:
        """Return an MCP/log-safe view: never host paths, env values, or secrets."""
        tools = {
            name: {"configured": getattr(self, name) is not None, "source": self.source_of(name).value}
            for name in sorted(_PATH_FIELDS)
        }
        return {
            "workspace": {"configured": True, "source": self.source_of("workspace_root").value},
            "source_dir": {"value": self.cmake_source_dir, "source": self.source_of("cmake_source_dir").value},
            "build_dir": {"value": self.build_dir or "build", "source": self.source_of("build_dir").value},
            "toolchain": {"value": self.toolchain, "source": self.source_of("toolchain").value},
            "host_arch": {"value": self.host_arch, "source": self.source_of("host_arch").value},
            "target_arch": {"value": self.target_arch, "source": self.source_of("target_arch").value},
            "visual_studio_instance": {"configured": self.visual_studio_instance is not None, "source": self.source_of("visual_studio_instance").value},
            "cmake": {
                "generator_configured": self.cmake_generator is not None,
                "preset_configured": self.configure_preset is not None,
                "configuration_configured": self.default_configuration is not None,
            },
            "timeouts": {"configure_seconds": self.configure_timeout_seconds, "build_seconds": self.build_timeout_seconds, "test_seconds": self.test_timeout_seconds},
            "log_level": {"value": self.log_level, "source": self.source_of("log_level").value},
            "tools": tools,
        }

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None, *, cwd: Path | None = None) -> "ForgeConfig":
        """Backward-compatible composition from process environment only."""
        return cls.from_sources(environment=environment, cwd=cwd)

    @classmethod
    def from_sources(
        cls, *, environment: Mapping[str, str] | None = None, cli: Mapping[str, Any] | None = None,
        cwd: Path | None = None,
    ) -> "ForgeConfig":
        """Compose CLI over environment over defaults exactly once."""
        values = _validated_environment(os.environ if environment is None else environment)
        command = {} if cli is None else dict(cli)
        current_directory = Path.cwd() if cwd is None else cwd
        sources: dict[str, ConfigurationSource] = {}

        def choose(name: str, variable: str, default: Any = None) -> Any:
            cli_value = command.get(name)
            if cli_value is not None:
                sources[name] = ConfigurationSource.CLI
                return cli_value
            env_value = values.get(variable)
            if env_value is not None and env_value.strip() != "":
                sources[name] = ConfigurationSource.ENVIRONMENT
                return env_value
            sources[name] = _SOURCE_DEFAULTS[name]
            return default

        workspace = choose("workspace_root", "FORGEMCP_WORKSPACE", current_directory)
        external_enabled = choose("external_plugins_enabled", "FORGEMCP_EXTERNAL_PLUGINS_ENABLED", "false")
        allowlist = choose("external_plugin_allowlist", "FORGEMCP_EXTERNAL_PLUGIN_ALLOWLIST", "")
        selected: dict[str, Any] = {
            "workspace_root": Path(workspace),
            "log_level": choose("log_level", "FORGEMCP_LOG_LEVEL", "INFO"),
            "external_plugins_enabled": _read_boolean(str(external_enabled), variable_name="FORGEMCP_EXTERNAL_PLUGINS_ENABLED"),
            "external_plugin_allowlist": frozenset(name.strip() for name in str(allowlist).split(",") if name.strip()),
            "cmake_source_dir": choose("cmake_source_dir", "FORGEMCP_SOURCE_DIR", "."),
            "build_dir": choose("build_dir", "FORGEMCP_BUILD_DIR", None),
            "toolchain": choose("toolchain", "FORGEMCP_TOOLCHAIN", "auto"),
            "host_arch": choose("host_arch", "FORGEMCP_HOST_ARCH", "auto"),
            "target_arch": choose("target_arch", "FORGEMCP_TARGET_ARCH", "auto"),
            "visual_studio_instance": choose("visual_studio_instance", "FORGEMCP_VISUAL_STUDIO_INSTANCE", None),
            "cmake_generator": choose("cmake_generator", "FORGEMCP_CMAKE_GENERATOR", None),
            "configure_preset": choose("configure_preset", "FORGEMCP_CONFIGURE_PRESET", None),
            "default_configuration": choose("default_configuration", "FORGEMCP_DEFAULT_CONFIGURATION", None),
            "configure_timeout_seconds": choose("configure_timeout_seconds", "FORGEMCP_CONFIGURE_TIMEOUT_SEC", 300.0),
            "build_timeout_seconds": choose("build_timeout_seconds", "FORGEMCP_BUILD_TIMEOUT_SEC", 900.0),
            "test_timeout_seconds": choose("test_timeout_seconds", "FORGEMCP_TEST_TIMEOUT_SEC", 900.0),
        }
        for name, variable in {
            "cmake_path": "FORGEMCP_CMAKE", "ctest_path": "FORGEMCP_CTEST", "clangd_path": "FORGEMCP_CLANGD",
            "clang_format_path": "FORGEMCP_CLANG_FORMAT", "clang_tidy_path": "FORGEMCP_CLANG_TIDY", "lldb_dap_path": "FORGEMCP_LLDB_DAP",
        }.items():
            raw = choose(name, variable, None)
            selected[name] = Path(raw) if raw is not None and str(raw).strip() else None
        selected["_sources"] = sources
        selected["_host_environment"] = values
        return cls(**selected)


def _normalise_tool_path(value: object, field_name: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        variable = {
            "clangd_path": "FORGEMCP_CLANGD", "lldb_dap_path": "FORGEMCP_LLDB_DAP",
            "clang_format_path": "FORGEMCP_CLANG_FORMAT", "clang_tidy_path": "FORGEMCP_CLANG_TIDY",
            "cmake_path": "FORGEMCP_CMAKE", "ctest_path": "FORGEMCP_CTEST",
        }[field_name]
        raise ConfigurationError(f"{variable} must be an absolute executable path.")
    return value.absolute()


def _normalise_relative_directory(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ConfigurationError(f"{field_name} must be a non-empty workspace-relative directory.")
    native, windows, posix = Path(value), PureWindowsPath(value), PurePosixPath(value)
    if native.is_absolute() or native.anchor or windows.drive or windows.root or windows.is_absolute() or posix.is_absolute():
        raise ConfigurationError(f"{field_name} must be workspace-relative.")
    parts = tuple(item for item in native.parts if item not in {"", "."})
    if any(item == ".." for item in parts):
        raise ConfigurationError(f"{field_name} must not contain parent traversal.")
    return "." if not parts else Path(*parts).as_posix()


def _validate_short_text(value: object, field_name: str) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value):
        raise ConfigurationError(f"{field_name} must be bounded non-empty text when supplied.")


def _validated_environment(environment: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(environment, Mapping):
        raise ConfigurationError("Environment must be a string mapping.")
    return {key: value for key, value in environment.items() if isinstance(key, str) and isinstance(value, str) and "\x00" not in key and "\x00" not in value}


def _read_boolean(value: str, *, variable_name: str) -> bool:
    normalised = value.strip().lower()
    if normalised in {"1", "true", "yes"}:
        return True
    if normalised in {"0", "false", "no", ""}:
        return False
    raise ConfigurationError(f"{variable_name} must be one of true/false, yes/no, or 1/0.")
