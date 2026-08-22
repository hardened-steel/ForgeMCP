"""Read-only discovery and Process Runtime qualification of standalone ``lldb-dap``.

This module deliberately has no DAP wire client, plugin, MCP tool, or
debugger-service dependency.  It establishes only that an operator-installed
adapter can be started and stopped using the policy Phase 1 would require.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import struct
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import StructuredLogger
from forgemcp.processes.errors import ProcessError
from forgemcp.processes.policy import ProcessPolicy
from forgemcp.processes.runtime import ProcessEnvironmentMode, ProcessRuntime

_VERSION = re.compile(
    r"(?:lldb(?:-dap)?|llvm)[^\r\n]{0,80}?\bversion\s+(?P<version>\d+(?:\.\d+)+(?:[-+][\w.]+)?)",
    re.IGNORECASE,
)
_LOADER_STATUS = 0xC0000135


@dataclass(frozen=True, slots=True)
class LldbDapCandidate:
    """One read-only discovery candidate and its approved dependency directories."""

    path: Path
    source: str
    companion_directories: tuple[Path, ...] = ()

    @property
    def canonical_path(self) -> Path:
        """Return a display/diagnostic canonical path without approving links.

        ``path`` deliberately preserves the lexical configured path because
        exact approval must be able to reject a configured symlink or reparse
        point rather than resolving through it first.
        """
        return self.path.resolve(strict=False)


@dataclass(frozen=True, slots=True)
class AdapterQualification:
    """Transport-neutral result of a bounded executable qualification."""

    adapter_id: str
    version: str | None
    executable_path: Path | None
    source: str
    available: bool
    process_tree_ownership: bool
    environment_isolated: bool
    confirmed_object_formats: tuple[str, ...]
    confirmed_debug_information_formats: tuple[str, ...]
    unverified_capabilities: tuple[str, ...]
    unavailable_reason: str | None = None
    version_probe_exit_code: int | None = None
    help_probe_exit_code: int | None = None
    controlled_start_exit_code: int | None = None


class LldbDapQualifier:
    """Discover and qualify only local standalone ``lldb-dap`` installations."""

    def __init__(
        self,
        config: ForgeConfig,
        logger: StructuredLogger,
        *,
        environment: Mapping[str, str] | None = None,
        runtime_factory: Callable[[ProcessPolicy], ProcessRuntime] | None = None,
    ) -> None:
        self._config = config
        self._logger = logger
        self._environment = dict(config.host_environment if environment is None else environment)
        self._runtime_factory = runtime_factory or self._create_runtime

    def discover(self) -> tuple[LldbDapCandidate, ...]:
        """Return local candidates in the deliberate discovery order without executing them."""
        raw: list[tuple[Path, str]] = []
        if self._config.lldb_dap_path is not None:
            # Legacy qualifier diagnostics retain this stable source label;
            # the path itself still comes only from immutable Core config.
            raw.append((self._config.lldb_dap_path, "FORGEMCP_LLDB_DAP"))

        path_candidate = shutil.which("lldb-dap", path=self._environment.get("PATH"))
        if path_candidate is None and os.name == "nt":
            path_candidate = shutil.which("lldb-dap.exe", path=self._environment.get("PATH"))
        if path_candidate is not None:
            raw.append((Path(path_candidate), "PATH"))

        raw.extend((path, "standalone LLVM") for path in self._standalone_llvm_paths())
        raw.extend((path, "Visual Studio LLVM") for path in self._visual_studio_paths())
        raw.extend((path, "VS Code LLVM/CodeLLDB") for path in self._vscode_llvm_paths())
        raw.extend((path, "other local LLVM toolchain") for path in self._other_toolchain_paths())

        candidates: list[LldbDapCandidate] = []
        seen: set[str] = set()
        for path, source in raw:
            key = self._candidate_key(path)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                LldbDapCandidate(
                    path=path.absolute(),
                    source=source,
                    companion_directories=self._companion_directories(path),
                )
            )
        return tuple(candidates)

    async def discover_and_qualify(self) -> tuple[AdapterQualification, ...]:
        """Discover then run each local candidate through the strict adapter policy."""
        qualifications: list[AdapterQualification] = []
        for candidate in self.discover():
            qualifications.append(await self.qualify(candidate))
        return tuple(qualifications)

    async def qualify(self, candidate: LldbDapCandidate) -> AdapterQualification:
        """Qualify a candidate using only the Process Runtime adapter launch path."""
        configured_path = candidate.path
        path = configured_path.resolve(strict=False)
        try:
            policy = ProcessPolicy(
                allowed_executables=frozenset(),
                allowed_executable_paths=frozenset({configured_path}),
                allow_environment_inheritance=False,
            )
        except ValueError:
            return self._unavailable(candidate, path, "not an approved existing regular executable")

        runtime = self._runtime_factory(policy)
        version: str | None = None
        version_probe_exit_code: int | None = None
        help_probe_exit_code: int | None = None
        controlled_start_exit_code: int | None = None
        try:
            version_result = await runtime.run_trusted_adapter(
                [str(configured_path), "--version"],
                approved_path_directories=candidate.companion_directories,
                timeout_seconds=5.0,
            )
            version_probe_exit_code = version_result.exit_code
            version = self._version_from_result(version_result.stdout.text, version_result.stderr.text)
            help_result = None
            if version_result.timed_out or version_result.exit_code != 0 or version is None:
                help_result = await runtime.run_trusted_adapter(
                    [str(configured_path), "--help"],
                    approved_path_directories=candidate.companion_directories,
                    timeout_seconds=5.0,
                )
                help_probe_exit_code = help_result.exit_code
                help_version = self._version_from_result(help_result.stdout.text, help_result.stderr.text)
                if help_result.timed_out or help_result.exit_code != 0 or help_version is None:
                    reason = self._probe_failure_reason(version_result.exit_code, help_result.exit_code)
                    if reason.startswith("Windows loader"):
                        missing = self._missing_pe_dependencies(path, candidate.companion_directories)
                        if missing:
                            reason = f"{reason} ({', '.join(missing[:3])})"
                    return self._unavailable(
                        candidate,
                        path,
                        reason,
                        version=version,
                        version_probe_exit_code=version_probe_exit_code,
                        help_probe_exit_code=help_probe_exit_code,
                    )
                version = help_version

            handle = await runtime.start_trusted_adapter(
                [str(configured_path)], approved_path_directories=candidate.companion_directories
            )
            try:
                # This intentionally does not speak DAP.  It proves the
                # real adapter process reaches a controlled running state and
                # is reaped through the same strict path Phase 1 will use.
                await asyncio.sleep(0.05)
                controlled_start_exit_code = handle.returncode
                if controlled_start_exit_code is not None:
                    return self._unavailable(
                        candidate,
                        path,
                        "adapter exited during controlled start",
                        version=version,
                        version_probe_exit_code=version_probe_exit_code,
                        help_probe_exit_code=help_probe_exit_code,
                        controlled_start_exit_code=controlled_start_exit_code,
                    )
                if not handle.required_ownership or not handle.ownership_established:
                    return self._unavailable(
                        candidate,
                        path,
                        "required process-tree ownership was not established",
                        version=version,
                        version_probe_exit_code=version_probe_exit_code,
                        help_probe_exit_code=help_probe_exit_code,
                    )
                if handle.environment_mode is not ProcessEnvironmentMode.SCRUBBED:
                    return self._unavailable(
                        candidate,
                        path,
                        "adapter environment isolation was not established",
                        version=version,
                        version_probe_exit_code=version_probe_exit_code,
                        help_probe_exit_code=help_probe_exit_code,
                    )
            finally:
                await handle.aclose()

            return AdapterQualification(
                adapter_id="lldb-dap",
                version=version,
                executable_path=path.resolve(strict=True),
                source=candidate.source,
                available=True,
                process_tree_ownership=True,
                environment_isolated=True,
                confirmed_object_formats=(),
                confirmed_debug_information_formats=(),
                unverified_capabilities=(
                    "DAP initialize/disconnect",
                    "launch and debuggee lifecycle",
                    "PE/COFF object support",
                    "DWARF debug-information support",
                    "all debugger capabilities",
                ),
                version_probe_exit_code=version_probe_exit_code,
                help_probe_exit_code=help_probe_exit_code,
                controlled_start_exit_code=controlled_start_exit_code,
            )
        except ProcessError:
            return self._unavailable(
                candidate,
                path,
                "could not start through the strict adapter policy",
                version=version,
                version_probe_exit_code=version_probe_exit_code,
                help_probe_exit_code=help_probe_exit_code,
                controlled_start_exit_code=controlled_start_exit_code,
            )
        finally:
            await runtime.aclose()

    def _create_runtime(self, policy: ProcessPolicy) -> ProcessRuntime:
        return ProcessRuntime(self._config, self._logger, policy=policy)

    @staticmethod
    def _version_from_result(stdout: str, stderr: str) -> str | None:
        match = _VERSION.search(stdout) or _VERSION.search(stderr)
        return None if match is None else match.group("version")

    @staticmethod
    def _probe_failure_reason(version_exit: int | None, help_exit: int | None) -> str:
        exits = (version_exit, help_exit)
        if any(exit_code is not None and exit_code & 0xFFFFFFFF == _LOADER_STATUS for exit_code in exits):
            return "Windows loader could not resolve a required DLL from approved companion directories"
        if any(exit_code is None for exit_code in exits):
            return "adapter probe timed out"
        return "adapter did not return a recognized successful version banner"

    @staticmethod
    def _unavailable(
        candidate: LldbDapCandidate,
        path: Path,
        reason: str,
        *,
        version: str | None = None,
        version_probe_exit_code: int | None = None,
        help_probe_exit_code: int | None = None,
        controlled_start_exit_code: int | None = None,
    ) -> AdapterQualification:
        return AdapterQualification(
            adapter_id="lldb-dap",
            version=version,
            executable_path=path if path.exists() else None,
            source=candidate.source,
            available=False,
            process_tree_ownership=False,
            environment_isolated=False,
            confirmed_object_formats=(),
            confirmed_debug_information_formats=(),
            unverified_capabilities=("all debugger capabilities",),
            unavailable_reason=reason,
            version_probe_exit_code=version_probe_exit_code,
            help_probe_exit_code=help_probe_exit_code,
            controlled_start_exit_code=controlled_start_exit_code,
        )

    def _standalone_llvm_paths(self) -> tuple[Path, ...]:
        directories = [
            Path(value) / "LLVM" / "bin"
            for value in self._program_files_roots()
        ]
        directories.extend(
            [
                Path("C:/LLVM/bin"),
                Path(self._environment.get("LOCALAPPDATA", "")) / "Programs" / "LLVM" / "bin",
            ]
        )
        return self._executables_in(directories)

    def _visual_studio_paths(self) -> tuple[Path, ...]:
        editions = ("Community", "Professional", "Enterprise", "BuildTools")
        versions = ("2022", "18", "2019")
        paths: list[Path] = []
        for root in self._program_files_roots():
            for version in versions:
                for edition in editions:
                    base = root / "Microsoft Visual Studio" / version / edition / "VC" / "Tools" / "Llvm"
                    paths.extend(base / architecture / self._adapter_name() for architecture in ("x64/bin", "ARM64/bin"))
        return tuple(path for path in paths if path.is_file())

    def _vscode_llvm_paths(self) -> tuple[Path, ...]:
        local = self._environment.get("USERPROFILE") or self._environment.get("HOME")
        if not local:
            return ()
        extensions = Path(local) / ".vscode" / "extensions"
        if not extensions.is_dir():
            return ()
        # The directory listing is intentionally shallow and read-only.  A
        # CodeLLDB installation is not accepted unless it contains the
        # standalone lldb-dap executable selected by this backend.
        paths: list[Path] = []
        for directory in extensions.iterdir():
            if not directory.is_dir() or not (
                directory.name.startswith("llvm-") or directory.name.startswith("vadimcn.vscode-lldb")
            ):
                continue
            paths.extend(
                directory / relative / self._adapter_name()
                for relative in ("bin", "llvm/bin", "adapter", "extension/bin")
            )
        return tuple(path for path in paths if path.is_file())

    def _other_toolchain_paths(self) -> tuple[Path, ...]:
        directories = [
            Path("C:/msys64/mingw64/bin"),
            Path("C:/msys64/ucrt64/bin"),
            Path("C:/ProgramData/chocolatey/lib/llvm/tools/llvm/bin"),
        ]
        return self._executables_in(directories)

    def _companion_directories(self, path: Path) -> tuple[Path, ...]:
        """Find only local loader directories that contain a static dependency.

        LLVM layouts commonly keep an adapter in ``bin`` and a dependency in a
        sibling ``lib`` directory.  The executable directory is always needed;
        every other selected directory must contain a bounded, read-only PE
        import.  This avoids inheriting a broad toolchain or Developer Shell
        PATH while still allowing an installed companion DLL directory.
        """
        adapter_directory = path.parent
        architecture_root = adapter_directory.parent
        toolchain_root = architecture_root.parent
        candidate_directories = (
            adapter_directory,
            architecture_root / "bin",
            architecture_root / "lib",
            toolchain_root / "bin",
            toolchain_root / "lib",
        )
        imports = set(_pe_imports(path)) if os.name == "nt" else set()
        existing = self._existing_directories(candidate_directories)
        if not imports:
            return tuple(directory for directory in existing if directory == adapter_directory.resolve(strict=False))
        return tuple(
            directory
            for directory in existing
            if directory == adapter_directory.resolve(strict=False)
            or any((directory / imported).is_file() for imported in imports)
        )

    def _program_files_roots(self) -> tuple[Path, ...]:
        values = [
            value
            for value in (
                self._environment.get("ProgramFiles"),
                self._environment.get("ProgramW6432"),
                self._environment.get("ProgramFiles(x86)"),
            )
            if value
        ]
        if os.name == "nt":
            values.extend(("C:/Program Files", "C:/Program Files (x86)"))
        seen: set[str] = set()
        roots: list[Path] = []
        for value in values:
            path = Path(value)
            key = str(path).casefold() if os.name == "nt" else str(path)
            if key not in seen:
                seen.add(key)
                roots.append(path)
        return tuple(roots)

    @staticmethod
    def _adapter_name() -> str:
        return "lldb-dap.exe" if os.name == "nt" else "lldb-dap"

    def _executables_in(self, directories: Iterable[Path]) -> tuple[Path, ...]:
        return tuple(
            directory / self._adapter_name()
            for directory in directories
            if (directory / self._adapter_name()).is_file()
        )

    @staticmethod
    def _existing_directories(directories: Iterable[Path]) -> tuple[Path, ...]:
        seen: set[str] = set()
        result: list[Path] = []
        for directory in directories:
            if not directory.is_dir():
                continue
            resolved = directory.resolve(strict=True)
            key = str(resolved).casefold() if os.name == "nt" else str(resolved)
            if key not in seen:
                seen.add(key)
                result.append(resolved)
        return tuple(result)

    @staticmethod
    def _candidate_key(path: Path) -> str:
        value = str(path.resolve(strict=False))
        return value.casefold() if os.name == "nt" else value

    def _missing_pe_dependencies(
        self, executable: Path, companion_directories: tuple[Path, ...]
    ) -> tuple[str, ...]:
        """Read the PE import table and report dependencies absent from local loader paths.

        The parser is deliberately bounded and read-only.  It reports only
        static imports that are absent from the executable, approved companion
        directories, and Windows system directory; delay-loaded or dynamically
        selected DLLs remain outside this diagnostic's certainty.
        """
        if os.name != "nt":
            return ()
        imports = _pe_imports(executable)
        if not imports:
            return ()
        system_root = self._base_environment_value("SystemRoot")
        directories = [executable.parent, *companion_directories]
        if system_root is not None:
            directories.extend((Path(system_root) / "System32", Path(system_root)))
        missing = [
            name
            for name in imports
            if not any((directory / name).is_file() for directory in directories)
        ]
        return tuple(missing)

    def _base_environment_value(self, key: str) -> str | None:
        if os.name != "nt":
            return self._environment.get(key)
        wanted = key.casefold()
        return next(
            (value for name, value in self._environment.items() if name.casefold() == wanted),
            None,
        )


def _pe_imports(path: Path) -> tuple[str, ...]:
    """Return bounded static PE import names or an empty tuple for malformed input."""
    try:
        with path.open("rb") as stream:
            data = stream.read(8 * 1024 * 1024)
    except OSError:
        return ()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return ()
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return ()
    try:
        section_count = struct.unpack_from("<H", data, pe_offset + 6)[0]
        optional_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
        optional_offset = pe_offset + 24
        magic = struct.unpack_from("<H", data, optional_offset)[0]
    except struct.error:
        return ()
    data_directories_offset = optional_offset + (112 if magic == 0x20B else 96 if magic == 0x10B else 0)
    if not data_directories_offset or data_directories_offset + 16 > len(data):
        return ()
    try:
        import_rva, import_size = struct.unpack_from("<II", data, data_directories_offset + 8)
    except struct.error:
        return ()
    if not import_rva or not import_size:
        return ()
    section_offset = optional_offset + optional_size
    sections: list[tuple[int, int, int, int]] = []
    for index in range(min(section_count, 96)):
        offset = section_offset + index * 40
        if offset + 40 > len(data):
            return ()
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", data, offset + 8)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset, raw_size))

    def offset_for_rva(rva: int) -> int | None:
        for virtual_address, span, raw_offset, raw_size in sections:
            if virtual_address <= rva < virtual_address + span:
                candidate = raw_offset + rva - virtual_address
                return candidate if candidate < len(data) and candidate < raw_offset + raw_size else None
        return None

    descriptor_offset = offset_for_rva(import_rva)
    if descriptor_offset is None:
        return ()
    names: list[str] = []
    for index in range(256):
        offset = descriptor_offset + index * 20
        if offset + 20 > len(data):
            break
        original_first_thunk, _, _, name_rva, first_thunk = struct.unpack_from("<IIIII", data, offset)
        if not any((original_first_thunk, name_rva, first_thunk)):
            break
        name_offset = offset_for_rva(name_rva)
        if name_offset is None:
            continue
        end = data.find(b"\0", name_offset, min(name_offset + 260, len(data)))
        if end == -1:
            continue
        try:
            name = data[name_offset:end].decode("ascii")
        except UnicodeDecodeError:
            continue
        if name and name not in names:
            names.append(name)
    return tuple(names)
