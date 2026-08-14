"""Fixed-surface clang-tidy discovery, check listing, and diagnostic extraction."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from forgemcp.core.config import ForgeConfig
from forgemcp.models import Diagnostic, FileSnapshot, Location, Position, ProcessResult, Range, Severity
from forgemcp.processes import ProcessError
from forgemcp.quality.clang_format import known_quality_candidates, process_summary
from forgemcp.quality.errors import QualityRequestError, QualityToolUnavailableError
from forgemcp.quality.models import (
    QualityToolInfo,
    TidyCheckList,
    TidyExecutionState,
    TidyRunResult,
)
from forgemcp.workspace import WorkspaceError, WorkspaceService


MAX_TIDY_FILES = 64
MAX_TIDY_CHECKS = 2_048
_SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cp", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl"})
_VERSION = re.compile(r"\b(?:clang-tidy|LLVM)\s+version\s+([^\r\n]+)", re.IGNORECASE)
_CHECK_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
_CHECK_PATTERN = re.compile(r"^[A-Za-z0-9_.*,+-]{1,1024}$")
# Greedy path matching deliberately preserves a Windows drive colon while the
# last numeric :line:column pair remains unambiguous.
_DIAGNOSTIC = re.compile(
    r"^(?P<path>.+):(?P<line>-?[0-9]+):(?P<column>-?[0-9]+):\s*"
    r"(?P<severity>warning|error|fatal error|note|remark):\s*(?P<message>.*?)(?:\s+\[(?P<check>[^\]\r\n]+)\])?\s*$",
    re.IGNORECASE,
)
_DIAGNOSTIC_LIKE = re.compile(r"^.+:-?[0-9]+:-?[0-9]+:\s*[^:]+:")
_UNSUPPORTED_DIAGNOSTIC_LIKE = re.compile(
    r"(?:^.+\(-?[0-9]+,-?[0-9]+\)\s*:\s*|^.+:-?[0-9]+:\s*)"
    r"(?:warning|error|fatal error|note|remark):",
    re.IGNORECASE,
)
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_QUOTED_ABSOLUTE_PATH = re.compile(
    r"(?P<quote>['\"])(?:[A-Za-z]:[\\/]|\\\\|/)[^'\"\r\n]*(?P=quote)"
)
_UNQUOTED_ABSOLUTE_PATH_TAIL = re.compile(
    r"(?<![A-Za-z0-9_.])(?:[A-Za-z]:[\\/]|\\\\|/(?![\s/]))[^\r\n]*$"
)
_MAX_DIAGNOSTIC_COORDINATE = 2_147_483_647


class ProcessRunner(Protocol):
    async def run(self, argv: Sequence[str], *, cwd: str = ".", timeout_seconds: float | None = None) -> ProcessResult:
        """Run one policy-bound argv process."""


@dataclass(frozen=True, slots=True)
class _ToolSelection:
    canonical: str
    version: str


class ClangTidyService:
    """Run only fixed clang-tidy modes against a trusted workspace compilation database."""

    def __init__(self, config: ForgeConfig, workspace: WorkspaceService, process_runtime: ProcessRunner) -> None:
        self._config = config
        self._workspace = workspace
        self._process_runtime = process_runtime

    async def status(self) -> QualityToolInfo:
        try:
            tool = await self._qualify()
        except QualityToolUnavailableError as error:
            return QualityToolInfo(available=False, error=error.message)
        return QualityToolInfo(executable=tool.canonical, available=True, version=tool.version)

    async def list_checks(self, checks: str | None = None) -> TidyCheckList:
        pattern = self._validate_checks(checks)
        tool = await self._qualify()
        argv = [tool.canonical, "--list-checks"]
        if pattern is not None:
            argv.append(f"--checks={pattern}")
        try:
            result = await self._process_runtime.run(argv, cwd=".", timeout_seconds=20.0)
        except ProcessError as error:
            raise QualityToolUnavailableError("clang-tidy is not available through the configured Process Runtime.") from error
        names = sorted({line.strip() for line in result.stdout.text.splitlines() if _CHECK_NAME.fullmatch(line.strip())})
        truncated = result.stdout.truncated or result.stderr.truncated or len(names) > MAX_TIDY_CHECKS
        return TidyCheckList(checks=tuple(names[:MAX_TIDY_CHECKS]), truncated=truncated, process=process_summary(result))

    async def run(
        self,
        *,
        paths: Iterable[str],
        compile_commands_dir: str,
        checks: str | None = None,
        timeout_seconds: float | None = None,
    ) -> TidyRunResult:
        source_paths = self._validate_paths(paths)
        pattern = self._validate_checks(checks)
        for path in source_paths:
            snapshot = self._workspace.get_snapshot(path)
            if not snapshot.exists:
                raise QualityRequestError("clang-tidy source paths must name existing workspace files.")
        generated = self._workspace.open_generated_directory(compile_commands_dir)
        if not generated.get_snapshot("compile_commands.json").exists:
            raise QualityRequestError("compile_commands_dir must contain compile_commands.json.")
        timeout = self._validate_timeout(timeout_seconds)
        tool = await self._qualify()
        argv = [tool.canonical, f"-p={generated.relative_path}"]
        if pattern is not None:
            argv.append(f"--checks={pattern}")
        # Prefix every validated relative source with an explicit relative
        # directory.  This prevents leading '-' options and '@response' files
        # without using clang-tidy's compiler-argument ``--`` delimiter.
        argv.extend(f".{os.sep}{path}" for path in source_paths)
        try:
            result = await self._process_runtime.run(argv, cwd=".", timeout_seconds=timeout)
        except ProcessError as error:
            raise QualityToolUnavailableError("clang-tidy is not available through the configured Process Runtime.") from error
        diagnostics, omitted, invalid, parser_truncated = self._parse_diagnostics(
            (result.stdout.text, result.stderr.text)
        )
        if result.timed_out:
            state = TidyExecutionState.TIMED_OUT
        elif result.exit_code != 0:
            state = TidyExecutionState.TOOL_FAILURE
        else:
            state = TidyExecutionState.COMPLETED
        truncated = parser_truncated or result.stdout.truncated or result.stderr.truncated
        return TidyRunResult(
            diagnostics=diagnostics,
            omitted_external_count=omitted,
            omitted_invalid_count=invalid,
            truncated=truncated,
            complete=not truncated and invalid == 0,
            execution_state=state,
            process=process_summary(result),
        )

    async def _qualify(self) -> _ToolSelection:
        candidates: list[str] = []
        if self._config.clang_tidy_path is not None:
            candidates.append(str(self._config.clang_tidy_path))
        else:
            candidates.append("clang-tidy")
            candidates.extend(str(path) for path in known_quality_candidates("clang-tidy"))
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
                or "USAGE: clang-tidy" not in help_text
                or "--list-checks" not in help_text
                or "--checks" not in help_text
            ):
                continue
            return _ToolSelection(canonical, version)
        raise QualityToolUnavailableError("clang-tidy is not available through the configured Process Runtime.")

    def _canonical(self, candidate: str) -> str | None:
        resolver = getattr(self._process_runtime, "resolve_executable", None)
        if resolver is None:
            return candidate
        try:
            return str(resolver(candidate))
        except ProcessError:
            return None

    @staticmethod
    def _validate_paths(paths: Iterable[str]) -> tuple[str, ...]:
        if isinstance(paths, (str, bytes)):
            raise QualityRequestError("clang-tidy paths must be an explicit bounded collection.")
        try:
            values = tuple(paths)
        except TypeError as error:
            raise QualityRequestError("clang-tidy paths must be an explicit bounded collection.") from error
        if not values or len(values) > MAX_TIDY_FILES or any(not isinstance(path, str) for path in values):
            raise QualityRequestError("clang-tidy requests must name from one through 64 explicit source files.")
        if any(not path or len(path) > 4096 or "\x00" in path for path in values):
            raise QualityRequestError("clang-tidy paths must be bounded NUL-free workspace paths.")
        identity_keys = tuple(os.path.normcase(os.path.normpath(path)) for path in values)
        if len(set(identity_keys)) != len(identity_keys):
            raise QualityRequestError("clang-tidy requests must not name a source file more than once.")
        if any(Path(path).suffix.lower() not in _SOURCE_SUFFIXES for path in values):
            raise QualityRequestError("clang-tidy supports only explicit C and C++ source-file extensions.")
        return values

    @staticmethod
    def _validate_checks(checks: str | None) -> str | None:
        if checks is None:
            return None
        if not isinstance(checks, str) or not _CHECK_PATTERN.fullmatch(checks) or "--" in checks:
            raise QualityRequestError("checks must be one bounded clang-tidy pattern without arguments.")
        return checks

    @staticmethod
    def _validate_timeout(timeout: float | None) -> float:
        if timeout is None:
            return 60.0
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
            raise QualityRequestError("timeout_seconds must be greater than zero and no more than 300.")
        return float(timeout)

    def _parse_diagnostics(
        self, text: str | Sequence[str]
    ) -> tuple[tuple[Diagnostic, ...], int, int, bool]:
        diagnostics: list[Diagnostic] = []
        omitted = 0
        invalid = 0
        truncated = False
        streams = (text,) if isinstance(text, str) else tuple(text)
        source_cache: dict[str, tuple[str, FileSnapshot] | None] = {}
        for stream in streams:
            lines = _strip_terminal_controls(stream).splitlines()
            for line in lines:
                match = _DIAGNOSTIC.match(line)
                if match is None:
                    if _DIAGNOSTIC_LIKE.match(line) or _UNSUPPORTED_DIAGNOSTIC_LIKE.match(line):
                        invalid += 1
                    continue
                location, location_state = self._normalise_location(
                    match.group("path"), match.group("line"), match.group("column"), source_cache
                )
                if location is None:
                    if location_state == "external":
                        omitted += 1
                    else:
                        invalid += 1
                    continue
                if len(diagnostics) >= MAX_TIDY_CHECKS:
                    truncated = True
                    continue
                severity_text = match.group("severity").casefold()
                severity = (
                    Severity.ERROR
                    if "error" in severity_text
                    else Severity.INFORMATION
                    if severity_text in {"note", "remark"}
                    else Severity.WARNING
                )
                check = match.group("check")
                code = check[:256] if check and _CHECK_NAME.fullmatch(check) else None
                diagnostics.append(
                    Diagnostic(
                        message=_safe_diagnostic_message(match.group("message")),
                        severity=severity,
                        location=location,
                        code=code,
                        source="clang-tidy",
                    )
                )
        return tuple(diagnostics), omitted, invalid, truncated

    def _normalise_location(
        self,
        reported_path: str,
        raw_line: str,
        raw_column: str,
        source_cache: dict[str, tuple[str, FileSnapshot] | None],
    ) -> tuple[Location | None, str]:
        """Convert Clang's one-based UTF-8 byte column to public code points."""
        try:
            relative = self._workspace.validate_reported_path(reported_path)
        except WorkspaceError:
            return None, "external"
        try:
            line_value = int(raw_line)
            column_value = int(raw_column)
        except ValueError:
            return None, "invalid"
        if not (
            1 <= line_value <= _MAX_DIAGNOSTIC_COORDINATE
            and 1 <= column_value <= _MAX_DIAGNOSTIC_COORDINATE
        ):
            return None, "invalid"
        cached = source_cache.get(relative)
        if relative not in source_cache:
            try:
                source, snapshot = self._workspace.read_text(relative)
            except WorkspaceError:
                source_cache[relative] = None
                return None, "invalid"
            cached = (source, snapshot)
            source_cache[relative] = cached
        if cached is None:
            return None, "invalid"
        source, snapshot = cached
        assert isinstance(source, str)
        source_lines = _source_lines(source)
        line_index = line_value - 1
        if line_index >= len(source_lines):
            return None, "invalid"
        byte_column = column_value - 1
        encoded_line = source_lines[line_index].encode("utf-8")
        if byte_column > len(encoded_line):
            return None, "invalid"
        try:
            code_point_column = len(encoded_line[:byte_column].decode("utf-8"))
        except UnicodeDecodeError:
            return None, "invalid"
        position = Position(line=line_index, column=code_point_column)
        return Location(uri=snapshot.uri, range=Range(start=position, end=position)), "valid"


def _strip_terminal_controls(text: str) -> str:
    """Strip bounded terminal control syntax before parsing or returning messages."""
    return _CONTROL.sub("", _ANSI_CSI.sub("", _ANSI_OSC.sub("", text)))


def _safe_diagnostic_message(message: str) -> str:
    """Bound one semantic message and redact absolute paths embedded within it."""
    value = message.strip() or "clang-tidy diagnostic"
    value = _QUOTED_ABSOLUTE_PATH.sub("'<external-path>'", value)
    value = _UNQUOTED_ABSOLUTE_PATH_TAIL.sub("<external-path>", value)
    return value[:16_384]


def _source_lines(source: str) -> tuple[str, ...]:
    """Return visible source lines while preserving a final empty EOF line."""
    raw_lines = source.splitlines(keepends=True)
    if not raw_lines:
        raw_lines = [""]
    elif source.endswith(("\n", "\r")):
        raw_lines.append("")
    visible: list[str] = []
    for line in raw_lines:
        if line.endswith("\r\n"):
            visible.append(line[:-2])
        elif line.endswith(("\n", "\r")):
            visible.append(line[:-1])
        else:
            visible.append(line)
    return tuple(visible)
