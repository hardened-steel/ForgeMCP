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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
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
_INSTANCE_VALUE = re.compile(r"^[A-Za-z0-9._:/\\ -]{1,256}$")
_ARCH_MACHINE = {"x86": 0x14C, "x64": 0x8664, "arm64": 0xAA64}
_SAFE_ENVIRONMENT_KEYS = frozenset({
    "PATH", "INCLUDE", "LIB", "LIBPATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
})


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
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._record_rejection("vswhere: malformed output")
            return ()
        if not isinstance(document, list) or len(document) > 64:
            self._record_rejection("vswhere: malformed output")
            return ()
        instances: list[VisualStudioInstance] = []
        for item in document:
            parsed = self._parse_vs_instance(item)
            if parsed is not None:
                instances.append(parsed)
        return tuple(sorted(instances, key=lambda item: (_version_key(item.installation_version), item.product_id, str(item.installation_path)), reverse=True))

    def _parse_vs_instance(self, value: object) -> VisualStudioInstance | None:
        if not isinstance(value, Mapping):
            self._record_rejection("visual_studio: malformed instance")
            return None
        raw_path = value.get("installationPath")
        product = value.get("productId", "VisualStudio")
        version = value.get("installationVersion", "0")
        display = value.get("displayName", product)
        if not all(isinstance(item, str) and _INSTANCE_VALUE.fullmatch(item) for item in (raw_path, product, version, display)):
            self._record_rejection("visual_studio: malformed instance")
            return None
        path = Path(raw_path)
        if not path.is_absolute() or _contains_link_or_reparse_point(path) or not path.is_dir():
            self._record_rejection("visual_studio: installation is unsafe or missing")
            return None
        packages = value.get("packages", [])
        components = frozenset(
            item.get("id") for item in packages
            if isinstance(item, Mapping) and isinstance(item.get("id"), str) and len(item["id"]) <= 256
        )
        dev_script = self._first_safe_file((path / "Common7" / "Tools" / "VsDevCmd.bat", path / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"))
        return VisualStudioInstance(path.resolve(), product, display, version, components, dev_script)

    def _select_visual_studio(self, instances: Sequence[VisualStudioInstance]) -> VisualStudioInstance | None:
        selector = self._config.visual_studio_instance
        if selector is not None:
            key = selector.casefold()
            selected = next((item for item in instances if key in {item.product_id.casefold(), item.display_name.casefold(), item.installation_version.casefold()}), None)
            if selected is None:
                self._record_rejection("visual_studio: requested instance was not found")
            elif not selected.has_vc_tools:
                self._record_rejection("visual_studio: selected instance is missing the VC tool workload")
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
        try:
            captured = self._capture_environment(self._selected_vs.dev_script, self._host_arch, self._target_arch)
            return self._filter_developer_environment(captured)
        except (OSError, subprocess.SubprocessError, ValueError):
            self._record_rejection("visual_studio: developer environment capture failed")
            return None

    def _filter_developer_environment(self, environment: Mapping[str, str]) -> Mapping[str, str]:
        if not isinstance(environment, Mapping) or len(environment) > _MAX_ENVIRONMENT_LINES:
            raise ValueError("invalid environment")
        filtered: dict[str, str] = {}
        for key, value in environment.items():
            if not isinstance(key, str) or not isinstance(value, str) or "\x00" in key or "\x00" in value or len(value) > _MAX_ENVIRONMENT_VALUE:
                raise ValueError("invalid environment")
            upper = key.upper()
            allowed = upper in _SAFE_ENVIRONMENT_KEYS or upper.startswith(("VC", "VS", "WINDOWSSDK", "UCRTVERSION", "UNIVERSALCRTSDKDIR", "WINDOWSLIBPATH"))
            if allowed:
                filtered[key] = value
        if not filtered.get("PATH"):
            raise ValueError("Developer environment did not supply PATH")
        return filtered

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
        values = (self._environment.get("ProgramFiles(x86)"), self._environment.get("ProgramFiles"), self._environment.get("ProgramW6432"))
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
                if path.is_absolute() and not _contains_link_or_reparse_point(path) and path.is_file():
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
        return any(name in self._environment for name in ("VSCMD_VER", "VCINSTALLDIR", "VSINSTALLDIR"))

    def _is_within_workspace(self, path: Path) -> bool:
        try:
            path.relative_to(self._config.workspace_root)
            return True
        except ValueError:
            return False

    def _record_rejection(self, message: str) -> None:
        if message not in self._rejections and len(self._rejections) < 64:
            self._rejections.append(message)

    @staticmethod
    def _default_run_vswhere(path: Path) -> bytes:
        completed = subprocess.run(
            [str(path), "-all", "-products", "*", "-prerelease", "-format", "json", "-utf8"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5, check=False, shell=False,
        )
        if completed.returncode != 0:
            raise subprocess.SubprocessError("vswhere failed")
        return completed.stdout

    def _default_capture_environment(self, script: Path, host_arch: str, target_arch: str) -> Mapping[str, str]:
        # ``script`` was discovered under a validated VS instance; architectures
        # are enum values. The command has no caller-controlled shell fragment.
        if script.name.casefold() == "vcvarsall.bat":
            command = f'call "{script}" {target_arch} >nul && set'
        else:
            command = f'call "{script}" -no_logo -host_arch={host_arch} -arch={target_arch} >nul && set'
        seed = {
            key: value
            for key, value in self._environment.items()
            if key.upper() in {
                "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT",
                "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
                "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
            }
        }
        system_root = next((value for key, value in seed.items() if key.upper() == "SYSTEMROOT"), None)
        if not system_root:
            raise subprocess.SubprocessError("Windows SystemRoot is unavailable")
        cmd_path, reason = self._safe_candidate(Path(system_root) / "System32" / "cmd.exe", "cmd")
        if cmd_path is None:
            raise subprocess.SubprocessError(f"trusted cmd.exe is unavailable ({reason})")
        # VsDevCmd may invoke fixed Windows helpers such as where.exe; retain
        # only System32 rather than inheriting host PATH.  ``cmd.exe`` needs a
        # raw Windows command line for the ``call`` batch syntax; every token
        # in it is either a checked script/path or an architecture enum.
        seed["PATH"] = str(Path(system_root) / "System32")
        command_line = f'"{cmd_path}" /d /s /c {command}'
        completed = subprocess.run(
            command_line,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=seed,
            timeout=15, check=False, shell=False,
        )
        if completed.returncode != 0 or len(completed.stdout) > _MAX_ENVIRONMENT_BYTES:
            raise subprocess.SubprocessError(f"Developer environment setup failed ({completed.returncode})")
        result: dict[str, str] = {}
        for line in completed.stdout.decode("utf-8", errors="strict").splitlines():
            if "=" not in line:
                raise ValueError("malicious environment line")
            key, value = line.split("=", 1)
            if not key or "\x00" in key or "\x00" in value:
                raise ValueError("malicious environment line")
            result[key] = value
        return result


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
