"""Central, fail-closed discovery for C++ development executables.

This module is intentionally transport-neutral. It never returns a host path in
its public diagnostic model; consumers obtain an exact ``Path`` only through the
application-scoped service. All discovery paths are validated with the same
non-link/reparse, workspace exclusion and metadata-capture guarantees used by
``ProcessPolicy``.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Final

from forgemcp.core.config import ConfigurationSource, ForgeConfig
from forgemcp.processes.policy import _contains_link_or_reparse_point


_TOOLS: Final = (
    "cmake", "ctest", "ninja", "msbuild", "cl", "clang", "clang++",
    "clangd", "clang-format", "clang-tidy", "lldb-dap", "cppvsdbg",
    "opendebugad7",
)
_DISPLAY_NAMES: Final = {"clang++": "clang++", "lldb-dap": "lldb-dap"}
_MAX_VSWHERE_BYTES = 512 * 1024
_MAX_ENVIRONMENT_BYTES = 256 * 1024
_MAX_ENVIRONMENT_LINES = 256
_MAX_ENVIRONMENT_VALUE = 16 * 1024
_MAX_ENVIRONMENT_NAME = 128
_MAX_VS_COMPONENTS = 256
_MAX_JSON_DEPTH = 16
_INSTANCE_VALUE = re.compile(r"^[^\x00-\x1f\x7f]{1,256}$")
# Windows has a small set of standard parenthesized variables such as
# PROGRAMFILES(X86).  '=' remains forbidden, so drive pseudo-variables (=C:)
# and malformed set output cannot enter the filtered environment.
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_()]{0,127}$")
_ARCH_MACHINE = {"x86": 0x14C, "x64": 0x8664, "arm64": 0xAA64}
_SAFE_ENVIRONMENT_KEYS = frozenset({
    "PATH", "INCLUDE", "LIB", "LIBPATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
})
_SECRET_ENVIRONMENT_PARTS = ("SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "COOKIE", "AUTH")
_CMD_UNSAFE_PATH_CHARACTERS = frozenset("&()%!^\"\r\n")
_ELIGIBLE_VS_PRODUCTS = ("community", "professional", "enterprise", "buildtools")


@dataclass(frozen=True, slots=True)
class ToolSelection:
    """Selected exact executable; its path remains internal to application code."""

    tool: str
    path: Path | None
    source: ConfigurationSource | str
    available: bool
    rejection: str | None = None

    def safe_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "available": self.available,
            "source": str(self.source),
            "rejection": self.rejection,
        }


@dataclass(frozen=True, slots=True)
class VisualStudioInstance:
    """Sanitized selected VS metadata; installation paths and IDs stay private."""

    installation_path: Path
    instance_id: str
    product_id: str
    display_name: str
    installation_version: str
    components: frozenset[str]
    dev_script: Path | None

    @property
    def has_vc_tools(self) -> bool:
        return any(
            component.startswith("Microsoft.VisualStudio.Component.VC.Tools")
            for component in self.components
        ) or (self.installation_path / "VC" / "Tools" / "MSVC").is_dir()


@dataclass(frozen=True, slots=True)
class ToolchainSnapshot:
    """Cached safe discovery state suitable for doctor and status providers."""

    toolchain: str
    host_arch: str
    target_arch: str
    visual_studio_available: bool
    visual_studio_vc_tools: bool
    developer_environment_available: bool
    tools: tuple[ToolSelection, ...]
    rejections: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "toolchain": self.toolchain,
            "host_arch": self.host_arch,
            "target_arch": self.target_arch,
            "visual_studio": {
                "available": self.visual_studio_available,
                "vc_tools_available": self.visual_studio_vc_tools,
                "developer_environment_available": self.developer_environment_available,
            },
            "tools": [item.safe_dict() for item in self.tools],
            "rejections": list(self.rejections),
        }


class ToolchainDiscoveryService:
    """Discover once per application and retain only exact approved candidates.

    A test can inject ``run_vswhere`` and ``capture_environment``. Production
    uses fixed executable/script invocations only; no user-controlled shell
    fragments are assembled or executed.
    """

    def __init__(
        self,
        config: ForgeConfig,
        *,
        run_vswhere: Callable[[Path], bytes] | None = None,
        capture_environment: Callable[[Path, str, str], Mapping[str, str]] | None = None,
    ) -> None:
        self._config = config
        self._environment = dict(config.host_environment)
        self._run_vswhere = run_vswhere or self._default_run_vswhere
        self._capture_environment = capture_environment or self._default_capture_environment
        self._host_arch = self._resolve_arch(config.host_arch)
        self._target_arch = self._resolve_arch(config.target_arch)
        self._selections: dict[str, ToolSelection] = {}
        self._rejections: list[str] = []
        self._instances: tuple[VisualStudioInstance, ...] = ()
        self._selected_vs: VisualStudioInstance | None = None
        self._developer_environment: Mapping[str, str] | None = None
        self.refresh()

    @property
    def approved_executable_paths(self) -> frozenset[Path]:
        return frozenset(item.path for item in self._selections.values() if item.path is not None)

    @property
    def toolchain_environment(self) -> Mapping[str, str] | None:
        """Bounded filtered Developer environment; never serialize or log it."""
        return self._developer_environment

    def executable(self, tool: str) -> Path | None:
        selection = self._selections.get(tool)
        return None if selection is None else selection.path

    def source(self, tool: str) -> str:
        selection = self._selections.get(tool)
        return "discovery" if selection is None else str(selection.source)

    def snapshot(self) -> ToolchainSnapshot:
        return ToolchainSnapshot(
            toolchain=self._config.toolchain,
            host_arch=self._host_arch,
            target_arch=self._target_arch,
            visual_studio_available=self._selected_vs is not None,
            visual_studio_vc_tools=bool(self._selected_vs and self._selected_vs.has_vc_tools),
            developer_environment_available=self._developer_environment is not None,
            tools=tuple(self._selections.get(name, ToolSelection(name, None, "discovery", False, "not found")) for name in _TOOLS),
            rejections=tuple(self._rejections[:64]),
        )

    def refresh(self) -> ToolchainSnapshot:
        """Perform bounded discovery and optional fixed Developer-shell capture."""
        self._rejections.clear()
        self._instances = self._discover_visual_studio()
        self._selected_vs = self._select_visual_studio(self._instances)
        self._developer_environment = self._discover_developer_environment()
        for tool in _TOOLS:
            self._selections[tool] = self._select_tool(tool)
        return self.snapshot()

    def _select_tool(self, tool: str) -> ToolSelection:
        rejected: list[str] = []
        candidates = self._candidates(tool)
        explicit_field = {
            "cmake": "cmake_path", "ctest": "ctest_path", "clangd": "clangd_path",
            "clang-format": "clang_format_path", "clang-tidy": "clang_tidy_path", "lldb-dap": "lldb_dap_path",
        }.get(tool)
        explicit_required = explicit_field is not None and getattr(self._config, explicit_field) is not None
        for index, (path, source) in enumerate(candidates):
            safe, reason = self._safe_candidate(path, tool)
            if safe is not None:
                return ToolSelection(tool, safe, source, True)
            if reason is not None:
                rejected.append(reason)
                self._record_rejection(f"{tool}: {reason}")
            # A supplied executable is an intentional operator selection, not
            # a hint. Do not silently replace a broken explicit path with a
            # lower-priority host tool.
            if explicit_required and index == 0:
                break
        reason = rejected[0] if rejected else "not found"
        self._record_rejection(f"{tool}: {reason}")
        return ToolSelection(tool, None, "discovery", False, reason)

    def _candidates(self, tool: str) -> Sequence[tuple[Path, ConfigurationSource | str]]:
        candidates: list[tuple[Path, ConfigurationSource | str]] = []
        configured_field = {
            "cmake": "cmake_path", "ctest": "ctest_path", "clangd": "clangd_path",
            "clang-format": "clang_format_path", "clang-tidy": "clang_tidy_path", "lldb-dap": "lldb_dap_path",
        }.get(tool)
        if configured_field is not None:
            configured = getattr(self._config, configured_field)
            if configured is not None:
                candidates.append((configured, self._config.source_of(configured_field)))
        # An existing Developer Shell takes precedence over VS discovery but
        # still has to pass exact path and workspace checks.
        if self._is_active_developer_environment():
            path = self._which(tool, self._environment.get("PATH"))
            if path is not None:
                candidates.append((path, "developer_environment"))
        if self._selected_vs is not None:
            candidates.extend((path, "visual_studio") for path in self._visual_studio_tool_paths(self._selected_vs, tool))
        path = self._which(tool, self._environment.get("PATH"))
        if path is not None:
            candidates.append((path, "path"))
        candidates.extend((path, "standalone") for path in self._standalone_tool_paths(tool))
        return candidates

    def _discover_visual_studio(self) -> tuple[VisualStudioInstance, ...]:
        if os.name != "nt":
            return ()
        vswhere = self._vswhere_path()
        if vswhere is None:
            self._record_rejection("vswhere: unavailable")
            return ()
        try:
            payload = self._run_vswhere(vswhere)
        except (OSError, subprocess.SubprocessError):
            self._record_rejection("vswhere: execution failed")
            return ()
        if not isinstance(payload, bytes) or len(payload) > _MAX_VSWHERE_BYTES:
            self._record_rejection("vswhere: malformed or oversized output")
            return ()
        try:
            document = json.loads(payload.decode("utf-8"))
        except (RecursionError, UnicodeDecodeError, json.JSONDecodeError):
            self._record_rejection("vswhere: malformed output")
            return ()
        if (
            not isinstance(document, list)
            or len(document) > 64
            or not _json_depth_within(document, _MAX_JSON_DEPTH)
        ):
            self._record_rejection("vswhere: malformed output")
            return ()
        instances: list[VisualStudioInstance] = []
        for item in document:
            parsed = self._parse_vs_instance(item)
            if parsed is not None:
                instances.append(parsed)
        unique: dict[tuple[str, str], VisualStudioInstance] = {}
        for instance in instances:
            key = (instance.instance_id.casefold(), _path_key(instance.installation_path))
            if key in unique:
                self._record_rejection("visual_studio: duplicate instance")
                continue
            unique[key] = instance
        return tuple(
            sorted(
                unique.values(),
                key=lambda item: (
                    _version_key(item.installation_version),
                    item.product_id.casefold(),
                    item.instance_id.casefold(),
                    _path_key(item.installation_path),
                ),
                reverse=True,
            )
        )

    def _parse_vs_instance(self, value: object) -> VisualStudioInstance | None:
        if not isinstance(value, Mapping):
            self._record_rejection("visual_studio: malformed instance")
            return None
        raw_path = value.get("installationPath")
        instance_id = value.get("instanceId")
        product = value.get("productId", "VisualStudio")
        version = value.get("installationVersion", "0")
        display = value.get("displayName", product)
        if not all(
            isinstance(item, str) and _INSTANCE_VALUE.fullmatch(item)
            for item in (raw_path, instance_id, product, version, display)
        ):
            self._record_rejection("visual_studio: malformed instance")
            return None
        if not any(product.casefold().endswith(kind) for kind in _ELIGIBLE_VS_PRODUCTS):
            self._record_rejection("visual_studio: unsupported product")
            return None
        path = Path(raw_path)
        if (
            not path.is_absolute()
            or _is_windows_special_path(path)
            or _contains_link_or_reparse_point(path)
            or not path.is_dir()
        ):
            self._record_rejection("visual_studio: installation is unsafe or missing")
            return None
        packages = value.get("packages", [])
        if not isinstance(packages, list) or len(packages) > _MAX_VS_COMPONENTS:
            self._record_rejection("visual_studio: malformed instance")
            return None
        components = frozenset(
            item.get("id") for item in packages
            if isinstance(item, Mapping) and isinstance(item.get("id"), str) and len(item["id"]) <= 256
        )
        dev_script = self._first_safe_file((path / "Common7" / "Tools" / "VsDevCmd.bat", path / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"))
        return VisualStudioInstance(path.resolve(), instance_id, product, display, version, components, dev_script)

    def _select_visual_studio(self, instances: Sequence[VisualStudioInstance]) -> VisualStudioInstance | None:
        selector = self._config.visual_studio_instance
        if selector is not None:
            key = selector.casefold()
            selected = next(
                (
                    item for item in instances
                    if key in {
                        item.instance_id.casefold(),
                        item.product_id.casefold(),
                        item.display_name.casefold(),
                        item.installation_version.casefold(),
                    }
                ),
                None,
            )
            if selected is None:
                self._record_rejection("visual_studio: requested instance was not found")
            elif not selected.has_vc_tools:
                self._record_rejection("visual_studio: selected instance is missing the VC tool workload")
                return None
            return selected
        if self._config.toolchain == "llvm":
            return next((item for item in instances if item.has_vc_tools), instances[0] if instances else None)
        selected = next((item for item in instances if item.has_vc_tools), instances[0] if instances else None)
        if selected is not None and not selected.has_vc_tools:
            self._record_rejection("visual_studio: selected instance is missing the VC tool workload")
        return selected

    def _discover_developer_environment(self) -> Mapping[str, str] | None:
        if os.name != "nt" or self._selected_vs is None or not self._selected_vs.has_vc_tools:
            return None
        if self._selected_vs.dev_script is None:
            self._record_rejection("visual_studio: developer command script is unavailable")
            return None
        if not _is_under(self._selected_vs.dev_script, self._selected_vs.installation_path):
            self._record_rejection("visual_studio: developer command script is unsafe")
            return None
        try:
            captured = self._capture_environment(self._selected_vs.dev_script, self._host_arch, self._target_arch)
            return self._filter_developer_environment(captured)
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError):
            self._record_rejection("visual_studio: developer environment capture failed")
            return None

    def _filter_developer_environment(self, environment: Mapping[str, str]) -> Mapping[str, str]:
        if not isinstance(environment, Mapping) or len(environment) > _MAX_ENVIRONMENT_LINES:
            raise ValueError("invalid environment")
        filtered: dict[str, str] = {}
        total_bytes = 0
        for key, value in environment.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or _ENVIRONMENT_NAME.fullmatch(key) is None
                or "\x00" in value
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                or len(value) > _MAX_ENVIRONMENT_VALUE
            ):
                raise ValueError("invalid environment")
            upper = key.upper()
            if upper in filtered or any(part in upper for part in _SECRET_ENVIRONMENT_PARTS):
                raise ValueError("invalid environment")
            allowed = (
                upper in _SAFE_ENVIRONMENT_KEYS
                or upper in {
                    "VCINSTALLDIR", "VCIDEINSTALLDIR", "VCTOOLSINSTALLDIR",
                    "VCTOOLSREDISTDIR", "VCTOOLSVERSION", "VSINSTALLDIR",
                    "VISUALSTUDIOVERSION",
                }
                or upper.startswith((
                    "VSCMD_", "VCTOOLS", "WINDOWSSDK", "WINDOWSLIBPATH",
                    "UCRTVERSION", "UNIVERSALCRTSDKDIR",
                ))
                or re.fullmatch(r"VS\d+COMNTOOLS", upper) is not None
            )
            if allowed:
                total_bytes += len(key.encode("utf-8")) + len(value.encode("utf-8")) + 1
                if total_bytes > _MAX_ENVIRONMENT_BYTES:
                    raise ValueError("invalid environment")
                filtered[upper] = value
        path = filtered.get("PATH")
        if not path:
            raise ValueError("Developer environment did not supply PATH")
        for name in ("PATH", "INCLUDE", "LIB", "LIBPATH"):
            if name in filtered:
                filtered[name] = self._filter_developer_path_list(filtered[name])
        return MappingProxyType(filtered)

    def _filter_developer_path_list(self, value: str) -> str:
        """Retain only existing, non-reparse VS/system toolchain directories."""
        parts = value.split(";")
        if not parts:
            raise ValueError("invalid developer path")
        accepted: list[Path] = []
        seen: set[str] = set()
        for raw in parts:
            if not raw or "\x00" in raw:
                raise ValueError("invalid developer path")
            directory = Path(raw)
            if (
                not directory.is_absolute()
                or _is_windows_special_path(directory)
                or _contains_link_or_reparse_point(directory)
                or not directory.is_dir()
                or self._is_within_workspace(directory.resolve())
                or not self._is_trusted_developer_directory(directory.resolve())
            ):
                raise ValueError("invalid developer path")
            key = _path_key(directory.resolve())
            if key not in seen:
                seen.add(key)
                accepted.append(directory.resolve())
        if not accepted:
            raise ValueError("invalid developer path")
        return ";".join(str(item) for item in accepted)

    def _is_trusted_developer_directory(self, directory: Path) -> bool:
        instance = self._selected_vs
        if instance is not None and _is_under(directory, instance.installation_path):
            return True
        system_root = self._environment_value("SystemRoot") or self._environment_value("WINDIR")
        if system_root:
            root = Path(system_root)
            if root.is_absolute() and not _is_windows_special_path(root) and _is_under(directory, root):
                return True
        # VsDevCmd legitimately adds SDK directories below the standard Program
        # Files roots.  Do not admit arbitrary host PATH entries merely because
        # they share a drive with a VS installation.
        return any(
            _is_under(directory, root / "Windows Kits")
            or _is_under(directory, root / "Microsoft SDKs")
            for root in self._program_files_roots()
        )

    def _visual_studio_tool_paths(self, instance: VisualStudioInstance, tool: str) -> tuple[Path, ...]:
        root = instance.installation_path
        cmake_root = root / "Common7" / "IDE" / "CommonExtensions" / "Microsoft" / "CMake"
        paths: list[Path] = []
        if tool in {"cmake", "ctest"}:
            paths.append(cmake_root / "CMake" / "bin" / f"{tool}.exe")
        elif tool == "ninja":
            paths.append(cmake_root / "Ninja" / "ninja.exe")
        elif tool == "msbuild":
            paths.extend((
                root / "MSBuild" / "Current" / "Bin" / "amd64" / "MSBuild.exe",
                root / "MSBuild" / "Current" / "Bin" / "MSBuild.exe",
            ))
        elif tool == "cl":
            paths.extend(root.glob(f"VC/Tools/MSVC/*/bin/Host{self._host_arch}/{self._target_arch}/cl.exe"))
        elif tool in {"clang", "clang++", "clangd", "clang-format", "clang-tidy", "lldb-dap"}:
            name = f"{_DISPLAY_NAMES.get(tool, tool)}.exe"
            paths.extend((root / "VC" / "Tools" / "Llvm" / architecture / "bin" / name) for architecture in ("x64", "ARM64", "x86"))
        elif tool in {"cppvsdbg", "opendebugad7"}:
            # Discovery-only: DAP backend selection remains the ADR 0009 LLVM baseline.
            names = ("cppvsdbg.exe", "OpenDebugAD7.exe")
            wanted = names[0] if tool == "cppvsdbg" else names[1]
            paths.append(root / "Common7" / "IDE" / "CommonExtensions" / "Microsoft" / "MIEngine" / "bin" / wanted)
        return tuple(paths)

    def _standalone_tool_paths(self, tool: str) -> tuple[Path, ...]:
        suffix = ".exe" if os.name == "nt" else ""
        name = _DISPLAY_NAMES.get(tool, tool) + suffix
        paths: list[Path] = []
        if self._config.toolchain != "msvc" or tool in {"cmake", "ctest", "ninja"}:
            for root in self._program_files_roots():
                paths.append(root / "LLVM" / "bin" / name)
            if os.name == "nt":
                paths.append(Path("C:/LLVM/bin") / name)
            else:
                paths.extend(Path(directory) / name for directory in ("/usr/bin", "/usr/local/bin", "/opt/llvm/bin"))
        return tuple(paths)

    def _safe_candidate(self, candidate: Path, tool: str) -> tuple[Path | None, str | None]:
        if not candidate.is_absolute():
            return None, "candidate is not absolute"
        if _is_windows_special_path(candidate):
            return None, "candidate uses an unsafe Windows path form"
        if _contains_link_or_reparse_point(candidate):
            return None, "candidate traverses a symlink or reparse point"
        try:
            canonical = candidate.resolve(strict=True)
            metadata = canonical.stat()
        except OSError:
            return None, "candidate is missing"
        if not stat.S_ISREG(metadata.st_mode):
            return None, "candidate is not a regular file"
        if self._is_within_workspace(canonical):
            return None, "candidate is inside the workspace"
        if os.name != "nt" and not os.access(canonical, os.X_OK):
            return None, "candidate is not executable"
        if os.name == "nt" and not self._is_machine_compatible(canonical):
            return None, "candidate architecture is incompatible"
        return canonical, None

    def _is_machine_compatible(self, path: Path) -> bool:
        machine = _read_pe_machine(path)
        return machine == _ARCH_MACHINE[self._host_arch]

    def _vswhere_path(self) -> Path | None:
        candidates = [root / "Microsoft Visual Studio" / "Installer" / "vswhere.exe" for root in self._program_files_roots()]
        candidates.append(Path("C:/Program Files (x86)/Microsoft Visual Studio/Installer/vswhere.exe"))
        return self._first_safe_file(candidates)

    def _program_files_roots(self) -> tuple[Path, ...]:
        values = (
            self._environment_value("ProgramFiles(x86)"),
            self._environment_value("ProgramFiles"),
            self._environment_value("ProgramW6432"),
        )
        roots = [Path(value) for value in values if value and Path(value).is_absolute()]
        return tuple(dict.fromkeys(roots))

    def _which(self, tool: str, path: str | None) -> Path | None:
        if not path:
            return None
        names = (tool, f"{tool}.exe") if os.name == "nt" and not tool.lower().endswith(".exe") else (tool,)
        for name in names:
            found = shutil.which(name, path=path)
            if found:
                return Path(found)
        return None

    @staticmethod
    def _first_safe_file(paths: Sequence[Path]) -> Path | None:
        for path in paths:
            try:
                if (
                    path.is_absolute()
                    and not _is_windows_special_path(path)
                    and not _contains_link_or_reparse_point(path)
                    and path.is_file()
                ):
                    return path
            except OSError:
                continue
        return None

    @staticmethod
    def _resolve_arch(value: str) -> str:
        if value != "auto":
            return value
        machine = platform.machine().lower()
        if machine in {"amd64", "x86_64"}:
            return "x64"
        if "arm64" in machine or "aarch64" in machine:
            return "arm64"
        return "x86"

    def _is_active_developer_environment(self) -> bool:
        return any(
            self._environment_value(name) is not None
            for name in ("VSCMD_VER", "VCINSTALLDIR", "VSINSTALLDIR")
        )

    def _environment_value(self, name: str) -> str | None:
        """Read one Windows environment name case-insensitively from the snapshot."""
        wanted = name.casefold()
        return next(
            (value for key, value in self._environment.items() if key.casefold() == wanted),
            None,
        )

    def _is_within_workspace(self, path: Path) -> bool:
        try:
            path.relative_to(self._config.workspace_root)
            return True
        except ValueError:
            return False

    def _record_rejection(self, message: str) -> None:
        if message not in self._rejections and len(self._rejections) < 64:
            self._rejections.append(message)

    def _default_run_vswhere(self, path: Path) -> bytes:
        return self._run_bounded_capture(
            [str(path), "-all", "-products", "*", "-prerelease", "-format", "json", "-utf8"],
            timeout_seconds=5.0,
            maximum_bytes=_MAX_VSWHERE_BYTES,
        )

    def _default_capture_environment(self, script: Path, host_arch: str, target_arch: str) -> Mapping[str, str]:
        # ``script`` was discovered under a validated VS instance; architectures
        # are enum values.  ``cmd.exe`` has no escaping form that safely proves
        # every metacharacter in a batch-file path inert, so reject those rare
        # installation paths before constructing its fixed command text.
        if (
            not script.is_absolute()
            or any(character in _CMD_UNSAFE_PATH_CHARACTERS for character in str(script))
        ):
            raise subprocess.SubprocessError("VsDevCmd path cannot be represented safely")
        if script.name.casefold() == "vcvarsall.bat":
            command = f'call "{script}" {target_arch} >nul && set'
        else:
            command = f'call "{script}" -no_logo -host_arch={host_arch} -arch={target_arch} >nul && set'
        seed = {
            key: value
            for key, value in self._environment.items()
            if key.upper() in {
                "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT",
                "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
            }
        }
        system_root = next((value for key, value in seed.items() if key.upper() == "SYSTEMROOT"), None)
        if not system_root:
            raise subprocess.SubprocessError("Windows SystemRoot is unavailable")
        cmd_path, reason = self._safe_candidate(Path(system_root) / "System32" / "cmd.exe", "cmd")
        if cmd_path is None:
            raise subprocess.SubprocessError(f"trusted cmd.exe is unavailable ({reason})")
        if any(character in _CMD_UNSAFE_PATH_CHARACTERS for character in str(cmd_path)):
            raise subprocess.SubprocessError("trusted cmd.exe path cannot be represented safely")
        # VsDevCmd may invoke fixed Windows helpers such as where.exe; retain
        # only System32 rather than inheriting host PATH.  The command passed
        # to cmd is fixed except for the already-qualified path and enum values.
        seed["PATH"] = str(Path(system_root) / "System32")
        # Python's Windows argv quoting adds an outer pair around the command
        # argument, which changes cmd.exe's `/s /c` quote-removal rules.  Use
        # the documented raw command line only after rejecting every cmd
        # metacharacter from the sole path embedded in it.
        command_line = f'"{cmd_path}" /d /s /c {command}'
        output = self._run_bounded_capture(
            command_line,
            timeout_seconds=15.0,
            maximum_bytes=_MAX_ENVIRONMENT_BYTES,
            environment=seed,
        )
        result: dict[str, str] = {}
        for line in output.decode("utf-8", errors="strict").splitlines():
            if "=" not in line:
                raise ValueError("malicious environment line")
            key, value = line.split("=", 1)
            if not key or "\x00" in key or "\x00" in value:
                raise ValueError("malicious environment line")
            result[key] = value
        return result

    def _run_bounded_capture(
        self,
        argv: Sequence[str] | str,
        *,
        timeout_seconds: float,
        maximum_bytes: int,
        environment: Mapping[str, str] | None = None,
    ) -> bytes:
        """Run a discovery helper with a streaming byte cap and tree cleanup.

        ``subprocess.run(..., stdout=PIPE)`` first accumulates all output, so a
        post-hoc size check is not a bound.  Discovery is synchronous during
        composition, therefore this small local helper owns the short-lived
        process directly and kills its Windows process tree on overflow or
        timeout before returning a fixed failure category.
        """
        kwargs: dict[str, object] = {"shell": False}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            argv if isinstance(argv, str) else list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=None if environment is None else dict(environment),
            **kwargs,
        )
        assert process.stdout is not None
        output = bytearray()
        output_lock = threading.Lock()
        overflow = threading.Event()
        read_failed = threading.Event()

        def read_stdout() -> None:
            try:
                while True:
                    chunk = process.stdout.read(65_536)
                    if not chunk:
                        return
                    with output_lock:
                        remaining = maximum_bytes - len(output)
                        if len(chunk) > remaining:
                            if remaining > 0:
                                output.extend(chunk[:remaining])
                            overflow.set()
                            return
                        output.extend(chunk)
            except OSError:
                read_failed.set()

        reader = threading.Thread(target=read_stdout, name="forgemcp-discovery-capture", daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        while process.poll() is None:
            if overflow.is_set() or read_failed.is_set() or time.monotonic() >= deadline:
                timed_out = time.monotonic() >= deadline
                self._terminate_discovery_process_tree(process)
                break
            time.sleep(0.01)
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._terminate_discovery_process_tree(process)
        reader.join(timeout=2.0)
        try:
            process.stdout.close()
        except OSError:
            pass
        if (
            timed_out
            or overflow.is_set()
            or read_failed.is_set()
            or reader.is_alive()
            or process.returncode != 0
        ):
            raise subprocess.SubprocessError("bounded discovery helper failed")
        return bytes(output)

    def _terminate_discovery_process_tree(self, process: subprocess.Popen[bytes]) -> None:
        """Best-effort Windows tree termination for timed-out discovery helpers."""
        if os.name == "nt":
            system_root = self._environment_value("SystemRoot") or self._environment_value("WINDIR")
            if system_root:
                taskkill, _ = self._safe_candidate(
                    Path(system_root) / "System32" / "taskkill.exe", "taskkill"
                )
                if taskkill is not None:
                    try:
                        subprocess.run(
                            [str(taskkill), "/pid", str(process.pid), "/t", "/f"],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5.0,
                            check=False,
                            shell=False,
                        )
                    except (OSError, subprocess.SubprocessError):
                        pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass


def _read_pe_machine(path: Path) -> int | None:
    """Read only the PE machine field; unknown/non-PE candidates are not trusted on Windows."""
    try:
        with path.open("rb") as stream:
            header = stream.read(0x40)
            if len(header) < 0x40 or header[:2] != b"MZ":
                return None
            offset = int.from_bytes(header[0x3C:0x40], "little")
            if offset > 1_048_576:
                return None
            stream.seek(offset)
            pe = stream.read(6)
            if len(pe) != 6 or pe[:4] != b"PE\0\0":
                return None
            return int.from_bytes(pe[4:6], "little")
    except OSError:
        return None


def _version_key(value: str) -> tuple[int, ...]:
    """Deterministic numeric ordering for normal VS installation versions."""
    return tuple(int(item) if item.isdigit() else -1 for item in value.split("."))


def _path_key(path: Path) -> str:
    """Return the platform-correct identity key for trusted filesystem paths."""
    value = str(path)
    return value.casefold() if os.name == "nt" else value


def _is_under(candidate: Path, root: Path) -> bool:
    """Containment with Windows' case-insensitive comparison semantics."""
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        if os.name != "nt":
            return False
    candidate_key = _path_key(candidate)
    root_key = _path_key(root).rstrip("\\/")
    return candidate_key == root_key or candidate_key.startswith(root_key + "\\") or candidate_key.startswith(root_key + "/")


def _is_windows_special_path(path: Path) -> bool:
    """Reject UNC and device namespaces, which do not have local-file guarantees."""
    raw = str(path)
    windows = PureWindowsPath(raw)
    return raw.startswith(("\\\\?\\", "\\\\.\\")) or windows.drive.startswith("\\\\")


def _json_depth_within(value: object, maximum: int) -> bool:
    """Bound nested vswhere data iteratively, avoiding parser-adjacent recursion."""
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        if depth > maximum:
            return False
        if isinstance(current, Mapping):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
    return True
