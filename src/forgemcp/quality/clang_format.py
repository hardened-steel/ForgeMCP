"""Safe clang-format qualification plus check/apply through Workspace CAS."""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
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


MAX_FORMAT_FILES = 64
"""Maximum explicitly named formatter targets per request."""

_SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cp", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl"})
_VERSION = re.compile(r"\bclang-format version\s+([^\r\n]+)", re.IGNORECASE)


class ProcessRunner(Protocol):
    """The intentionally small ProcessRuntime surface used by formatter services."""

    async def run(self, argv: Sequence[str], *, cwd: str = ".", timeout_seconds: float | None = None) -> ProcessResult:
        """Run a bounded argv process."""


@dataclass(frozen=True, slots=True)
class _ToolSelection:
    requested: str
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


def known_quality_candidates(tool_name: str) -> tuple[Path, ...]:
    """Return only conventional operator-installed LLVM locations, never a scan."""
    if os.name != "nt":
        return ()
    roots = tuple(
        Path(value)
        for value in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"))
        if value
    )
    return tuple(root / "LLVM" / "bin" / f"{tool_name}.exe" for root in roots)


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

    def __init__(self, config: ForgeConfig, workspace: WorkspaceService, process_runtime: ProcessRunner) -> None:
        self._config = config
        self._workspace = workspace
        self._process_runtime = process_runtime

    async def status(self) -> QualityToolInfo:
        """Probe the fixed policy-approved executable without making startup depend on it."""
        try:
            selected = await self._qualify()
        except QualityToolUnavailableError as error:
            return QualityToolInfo(available=False, error=error.message)
        return QualityToolInfo(executable=selected.canonical, available=True, version=selected.version)

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
        if self._config.clang_format_path is not None:
            candidates.append(str(self._config.clang_format_path))
        else:
            candidates.append("clang-format")
            candidates.extend(str(path) for path in known_quality_candidates("clang-format"))
        for candidate in candidates:
            canonical = self._canonical(candidate)
            if canonical is None:
                continue
            try:
                result = await self._process_runtime.run([candidate, "--version"], cwd=".", timeout_seconds=5.0)
            except ProcessError:
                continue
            if result.timed_out or result.exit_code != 0 or result.stdout.truncated or result.stderr.truncated:
                continue
            match = _VERSION.search(result.stdout.text) or _VERSION.search(result.stderr.text)
            if match is not None:
                return _ToolSelection(candidate, canonical, match.group(1).strip()[:256])
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
        if len(set(values)) != len(values):
            raise QualityRequestError("Formatter requests must not name a source file more than once.")
        for path in values:
            if Path(path).suffix.lower() not in _SOURCE_SUFFIXES:
                raise QualityRequestError("Formatter supports only explicit C and C++ source-file extensions.")
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
                [selected.requested, "--output-replacements-xml", path], cwd=".", timeout_seconds=30.0
            )
            summary = process_summary(result)
            if result.timed_out or result.exit_code != 0 or result.stdout.truncated:
                return _FailedFormattedFile(FormatFileResult(path=path, snapshot_sha256=snapshot.sha256, process=summary, error="clang-format could not produce a complete structured result."))
            replacements = self._parse_replacements(result.stdout.text)
            formatted = self._apply_replacements(source, replacements)
            positions = _byte_positions(source)
            if any(item.offset not in positions or item.offset + item.length not in positions for item in replacements):
                raise ValueError("clang-format replacement did not align to UTF-8 source boundaries.")
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
    def _parse_replacements(output: str) -> tuple[_Replacement, ...]:
        root = ET.fromstring(output)
        if root.tag != "replacements" or root.attrib.get("incomplete_format") == "true":
            raise ValueError("clang-format did not return complete replacement XML.")
        replacements: list[_Replacement] = []
        previous_end = -1
        previous_start = -1
        for node in root:
            if node.tag != "replacement":
                raise ValueError("clang-format replacement XML was malformed.")
            offset = int(node.attrib["offset"])
            length = int(node.attrib["length"])
            if offset < 0 or length < 0 or offset < previous_end or offset == previous_start:
                raise ValueError("clang-format replacement ranges overlap.")
            text = node.text or ""
            if "\x00" in text:
                raise ValueError("clang-format replacement XML contains NUL.")
            replacements.append(_Replacement(offset, length, text))
            previous_end = offset + length
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
