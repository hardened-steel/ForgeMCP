"""Safe clang-format qualification plus check/apply through Workspace CAS."""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from forgemcp.core.config import ForgeConfig
from forgemcp.models import FileSnapshot, Position, ProcessResult, Range
from forgemcp.processes import ProcessError
from forgemcp.quality.errors import QualityRequestError, QualityToolExecutionError, QualityToolUnavailableError
from forgemcp.quality.models import (
    FormatApplyResult,
    FormatCheckResult,
    FormatFileResult,
    QualityProcessSummary,
    QualityToolInfo,
)
from forgemcp.workspace import WorkspaceError, WorkspaceService, WorkspaceTextEdit
from forgemcp.toolchain import ToolchainDiscoveryService


MAX_FORMAT_FILES = 64
"""Maximum explicitly named formatter targets per request."""

MAX_FORMAT_XML_CHARACTERS = 65_536
"""Independent parser bound for untrusted clang-format XML."""

_SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cp", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl"})
_VERSION = re.compile(r"\bclang-format version\s+([^\r\n]+)", re.IGNORECASE)


class ProcessRunner(Protocol):
    """The intentionally small ProcessRuntime surface used by formatter services."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        timeout_seconds: float | None = None,
        input_data: bytes | None = None,
    ) -> ProcessResult:
        """Run a bounded argv process."""


@dataclass(frozen=True, slots=True)
class _ToolSelection:
    canonical: str
    version: str


@dataclass(frozen=True, slots=True)
class _Replacement:
    offset: int
    length: int
    text: str


@dataclass(frozen=True, slots=True)
class _FormattedFile:
    result: FormatFileResult
    snapshot: FileSnapshot
    source_text: str
    replacements: tuple[_Replacement, ...]


def known_quality_candidates(tool_name: str, environment: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Return only conventional operator-installed LLVM locations, never a scan."""
    if os.name == "nt":
        roots = tuple(
            Path(value)
            for value in (
                (environment or {}).get("ProgramFiles"),
                (environment or {}).get("ProgramW6432"),
                (environment or {}).get("ProgramFiles(x86)"),
            )
            if value
        )
        return tuple(root / "LLVM" / "bin" / f"{tool_name}.exe" for root in roots)
    return tuple(directory / tool_name for directory in (Path("/usr/bin"), Path("/usr/local/bin"), Path("/opt/llvm/bin")))


def process_summary(result: ProcessResult) -> QualityProcessSummary:
    """Project a raw-output process result into log-safe public completion facts."""
    return QualityProcessSummary(
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        stdout_truncated=result.stdout.truncated,
        stderr_truncated=result.stderr.truncated,
    )


class ClangFormatService:
    """Check and apply project-owned clang-format rules without ``-i`` or shell access."""

    def __init__(
        self, config: ForgeConfig, workspace: WorkspaceService, process_runtime: ProcessRunner,
        toolchain: ToolchainDiscoveryService | None = None,
    ) -> None:
        self._config = config
        self._workspace = workspace
        self._process_runtime = process_runtime
        self._toolchain = toolchain
        self._cached_status: QualityToolInfo | None = None

    @property
    def cached_status(self) -> QualityToolInfo | None:
        """Return the last qualification result without launching a probe."""

        return self._cached_status

    async def status(self) -> QualityToolInfo:
        """Probe the fixed policy-approved executable without making startup depend on it."""
        try:
            selected = await self._qualify()
        except QualityToolUnavailableError as error:
            self._cached_status = QualityToolInfo(available=False, error=error.message)
            return self._cached_status
        self._cached_status = QualityToolInfo(executable="clang-format", available=True, version=selected.version)
        return self._cached_status

    async def check(self, paths: Iterable[str]) -> FormatCheckResult:
        """Return one content-free comparison result per explicit workspace source file."""
        normalised = self._validate_paths(paths)
        selected = await self._qualify_or_raise()
        formatted = [await self._format_one(selected, path) for path in normalised]
        results = tuple(item.result for item in formatted)
        return FormatCheckResult(files=results, clean=all(item.error is None and item.would_change is False for item in results))

    async def apply(self, files: Iterable[tuple[str, str]]) -> FormatApplyResult:
        """Format every file first, then commit one guarded Workspace text-edit batch."""
        requested = self._validate_apply_files(files)
        selected = await self._qualify_or_raise()
        formatted = [await self._format_one(selected, path) for path, _ in requested]
        results = tuple(item.result for item in formatted)
        if any(item.result.error is not None for item in formatted):
            return FormatApplyResult(applied=False, files=results)
        if any(item.snapshot.sha256 != expected for item, (_, expected) in zip(formatted, requested, strict=True)):
            return FormatApplyResult(applied=False, files=results, conflict=True)

        # Guard no-op files too: the public apply result covers every requested
        # snapshot even though Workspace needs edits only for changing files.
        try:
            if any(
                self._workspace.get_snapshot(item.result.path).sha256 != item.snapshot.sha256
                for item in formatted
            ):
                return FormatApplyResult(applied=False, files=results, conflict=True)
        except WorkspaceError:
            return FormatApplyResult(applied=False, files=results, conflict=True)

        edits_by_path: dict[str, tuple[WorkspaceTextEdit, ...]] = {}
        expected_snapshots: dict[str, FileSnapshot] = {}
        for item in formatted:
            if not item.replacements:
                continue
            edits_by_path[item.result.path] = self._workspace_edits(item.source_text, item.replacements)
            expected_snapshots[item.result.path] = item.snapshot
        if not edits_by_path:
            return FormatApplyResult(applied=True, files=results)
        try:
            patch = self._workspace.apply_text_edits(edits_by_path, expected_snapshots)
        except WorkspaceError as error:
            raise QualityToolExecutionError("Formatting could not be committed through the workspace policy.") from error
        return FormatApplyResult(applied=patch.applied, files=results, conflict=not patch.applied)

    async def _qualify_or_raise(self) -> _ToolSelection:
        try:
            return await self._qualify()
        except QualityToolUnavailableError:
            raise

    async def _qualify(self) -> _ToolSelection:
        """Try configured path, policy-controlled PATH, then fixed LLVM candidates."""
        candidates: list[str] = []
        if self._toolchain is not None:
            selected = self._toolchain.executable("clang-format")
            if selected is not None:
                candidates.append(str(selected))
        elif self._config.clang_format_path is not None:
            candidates.append(str(self._config.clang_format_path))
        else:
            candidates.append("clang-format")
            candidates.extend(str(path) for path in known_quality_candidates("clang-format", self._config.host_environment))
        for candidate in candidates:
            canonical = self._canonical(candidate)
            if canonical is None:
                continue
            try:
                result = await self._process_runtime.run([canonical, "--version"], cwd=".", timeout_seconds=5.0)
            except ProcessError:
                continue
            if result.timed_out or result.exit_code != 0 or result.stdout.truncated or result.stderr.truncated:
                continue
            match = _VERSION.search(result.stdout.text) or _VERSION.search(result.stderr.text)
            version = match.group(1).strip() if match is not None else ""
            if not version or len(version) > 256:
                continue
            try:
                help_result = await self._process_runtime.run(
                    [canonical, "--help"], cwd=".", timeout_seconds=5.0
                )
            except ProcessError:
                continue
            help_text = help_result.stdout.text + help_result.stderr.text
            if (
                help_result.timed_out
                or help_result.exit_code != 0
                or help_result.stdout.truncated
                or help_result.stderr.truncated
                or "--output-replacements-xml" not in help_text
                or "--assume-filename" not in help_text
            ):
                continue
            selection = _ToolSelection(canonical, version)
            self._cached_status = QualityToolInfo(
                executable="clang-format", available=True, version=selection.version
            )
            return selection
        self._cached_status = QualityToolInfo(
            available=False,
            error="clang-format is not available through the configured Process Runtime.",
        )
        raise QualityToolUnavailableError("clang-format is not available through the configured Process Runtime.")

    def _canonical(self, candidate: str) -> str | None:
        resolver = getattr(self._process_runtime, "resolve_executable", None)
        if resolver is None:
            return candidate
        try:
            return str(resolver(candidate))
        except ProcessError:
            return None

    def _validate_paths(self, paths: Iterable[str]) -> tuple[str, ...]:
        if isinstance(paths, (str, bytes)):
            raise QualityRequestError("Formatter paths must be an explicit bounded collection.")
        try:
            values = tuple(paths)
        except TypeError as error:
            raise QualityRequestError("Formatter paths must be an explicit bounded collection.") from error
        if not values or len(values) > MAX_FORMAT_FILES or any(not isinstance(path, str) for path in values):
            raise QualityRequestError("Formatter requests must name from one through 64 explicit source files.")
        identity_keys = tuple(os.path.normcase(os.path.normpath(path)) for path in values)
        if len(set(identity_keys)) != len(identity_keys):
            raise QualityRequestError("Formatter requests must not name a source file more than once.")
        for path in values:
            if not path or len(path) > 4096 or "\x00" in path:
                raise QualityRequestError("Formatter paths must be bounded NUL-free workspace paths.")
            if Path(path).suffix.lower() not in _SOURCE_SUFFIXES:
                raise QualityRequestError("Formatter supports only explicit C and C++ source-file extensions.")
            try:
                self._workspace.get_snapshot(path)
            except WorkspaceError as error:
                raise QualityRequestError(
                    "Formatter paths must be valid workspace-relative non-link paths."
                ) from error
        return values

    def _validate_apply_files(self, files: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
        if isinstance(files, (str, bytes)):
            raise QualityRequestError("Formatter apply files must be a bounded collection.")
        try:
            values = tuple(files)
        except TypeError as error:
            raise QualityRequestError("Formatter apply files must be a bounded collection.") from error
        if not values or len(values) > MAX_FORMAT_FILES:
            raise QualityRequestError("Formatter apply requests must contain from one through 64 files.")
        paths: list[str] = []
        normalised: list[tuple[str, str]] = []
        for value in values:
            if not isinstance(value, tuple) or len(value) != 2 or not isinstance(value[0], str) or not isinstance(value[1], str):
                raise QualityRequestError("Each formatter apply file requires a path and expected SHA-256.")
            path, expected = value
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise QualityRequestError("Each formatter apply file requires a lowercase SHA-256 snapshot.")
            paths.append(path)
            normalised.append((path, expected))
        self._validate_paths(paths)
        return tuple(normalised)

    async def _format_one(self, selected: _ToolSelection, path: str) -> _FormattedFile | _FailedFormattedFile:
        try:
            source, snapshot = self._workspace.read_text(path)
            result = await self._process_runtime.run(
                [
                    selected.canonical,
                    "--output-replacements-xml",
                    f"--assume-filename={path}",
                ],
                cwd=".",
                timeout_seconds=30.0,
                input_data=source.encode("utf-8"),
            )
            summary = process_summary(result)
            if result.timed_out or result.exit_code != 0 or result.stdout.truncated or result.stderr.truncated:
                return _FailedFormattedFile(FormatFileResult(path=path, snapshot_sha256=snapshot.sha256, process=summary, error="clang-format could not produce a complete structured result."))
            if not source and not result.stdout.text:
                replacements: tuple[_Replacement, ...] = ()
            else:
                replacements = self._parse_replacements(
                    result.stdout.text, source_size=len(source.encode("utf-8"))
                )
            positions = _byte_positions(source)
            if any(item.offset not in positions or item.offset + item.length not in positions for item in replacements):
                raise ValueError("clang-format replacement did not align to UTF-8 source boundaries.")
            formatted = self._apply_replacements(source, replacements)
            return _FormattedFile(
                result=FormatFileResult(
                    path=path,
                    snapshot_sha256=snapshot.sha256,
                    would_change=formatted != source,
                    formatted_sha256=hashlib.sha256(formatted.encode("utf-8")).hexdigest(),
                    process=summary,
                ),
                snapshot=snapshot,
                source_text=source,
                replacements=replacements,
            )
        except (WorkspaceError, ProcessError, ET.ParseError, UnicodeError, ValueError):
            return _FailedFormattedFile(FormatFileResult(path=path, error="The requested file could not be formatted safely."))

    @staticmethod
    def _parse_replacements(output: str, *, source_size: int) -> tuple[_Replacement, ...]:
        if len(output) > MAX_FORMAT_XML_CHARACTERS:
            raise ValueError("clang-format replacement XML exceeds the parser limit.")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", output, re.IGNORECASE):
            raise ValueError("clang-format replacement XML must not contain DTDs or entities.")
        root = ET.fromstring(output)
        xml_space = "{http://www.w3.org/XML/1998/namespace}space"
        if root.tag != "replacements" or set(root.attrib) - {xml_space, "incomplete_format"}:
            raise ValueError("clang-format replacement XML has an unexpected root.")
        if root.attrib.get(xml_space, "preserve") != "preserve":
            raise ValueError("clang-format replacement XML has invalid whitespace policy.")
        incomplete = root.attrib.get("incomplete_format")
        if incomplete not in {None, "false", "true"} or incomplete == "true":
            raise ValueError("clang-format did not return complete replacement XML.")
        if root.text is not None and root.text.strip():
            raise ValueError("clang-format replacement XML has unexpected root text.")
        replacements: list[_Replacement] = []
        previous_end = -1
        previous_start = -1
        for node in root:
            if node.tag != "replacement":
                raise ValueError("clang-format replacement XML was malformed.")
            if set(node.attrib) != {"offset", "length"} or len(node):
                raise ValueError("clang-format replacement XML has invalid replacement attributes.")
            raw_offset = node.attrib["offset"]
            raw_length = node.attrib["length"]
            if not raw_offset.isascii() or not raw_offset.isdecimal() or len(raw_offset) > 20:
                raise ValueError("clang-format replacement offset is invalid.")
            if not raw_length.isascii() or not raw_length.isdecimal() or len(raw_length) > 20:
                raise ValueError("clang-format replacement length is invalid.")
            offset = int(raw_offset)
            length = int(raw_length)
            end = offset + length
            if end > source_size:
                raise ValueError("clang-format replacement is outside the source file.")
            if offset < 0 or length < 0 or offset < previous_end or offset == previous_start:
                raise ValueError("clang-format replacement ranges overlap.")
            text = node.text or ""
            if "\x00" in text:
                raise ValueError("clang-format replacement XML contains NUL.")
            if node.tail is not None and node.tail.strip():
                raise ValueError("clang-format replacement XML has unexpected trailing text.")
            replacements.append(_Replacement(offset, length, text))
            previous_end = end
            previous_start = offset
        return tuple(replacements)

    @staticmethod
    def _apply_replacements(source: str, replacements: tuple[_Replacement, ...]) -> str:
        data = source.encode("utf-8")
        output = data
        for replacement in reversed(replacements):
            end = replacement.offset + replacement.length
            if end > len(data):
                raise ValueError("clang-format replacement is outside the source file.")
            output = output[:replacement.offset] + replacement.text.encode("utf-8") + output[end:]
        return output.decode("utf-8")

    @staticmethod
    def _workspace_edits(source: str, replacements: tuple[_Replacement, ...]) -> tuple[WorkspaceTextEdit, ...]:
        positions = _byte_positions(source)
        return tuple(
            WorkspaceTextEdit(
                range=Range(start=positions[item.offset], end=positions[item.offset + item.length]),
                new_text=item.text,
            )
            for item in replacements
        )


@dataclass(frozen=True, slots=True)
class _FailedFormattedFile:
    """Internal failed format shape that deliberately cannot produce edits."""

    result: FormatFileResult

    snapshot: FileSnapshot | None = None
    source_text: str = ""
    replacements: tuple[_Replacement, ...] = ()


def _byte_positions(source: str) -> dict[int, Position]:
    """Map clang-format UTF-8 byte offsets to Workspace Unicode-code-point positions."""
    positions: dict[int, Position] = {0: Position(line=0, column=0)}
    byte_offset = 0
    line = 0
    column = 0
    index = 0
    while index < len(source):
        character = source[index]
        encoded = character.encode("utf-8")
        if character == "\r" and index + 1 < len(source) and source[index + 1] == "\n":
            positions[byte_offset] = Position(line=line, column=column)
            byte_offset += 2
            line += 1
            column = 0
            positions[byte_offset] = Position(line=line, column=column)
            index += 2
            continue
        positions[byte_offset] = Position(line=line, column=column)
        byte_offset += len(encoded)
        if character in {"\n", "\r"}:
            line += 1
            column = 0
        else:
            column += 1
        positions[byte_offset] = Position(line=line, column=column)
        index += 1
    return positions
