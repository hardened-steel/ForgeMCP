"""Fixed-surface clang-tidy discovery, check listing, and diagnostic extraction."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from forgemcp.core.config import ForgeConfig
from forgemcp.models import Diagnostic, Location, Position, ProcessResult, Range, Severity
from forgemcp.processes import ProcessError
from forgemcp.quality.clang_format import known_quality_candidates, process_summary
from forgemcp.quality.errors import QualityRequestError, QualityToolUnavailableError
from forgemcp.quality.models import (
    QualityProcessSummary,
    QualityToolInfo,
    TidyCheckList,
    TidyExecutionState,
    TidyRunResult,
)
from forgemcp.workspace import WorkspaceError, WorkspaceFileNotFoundError, WorkspaceService


MAX_TIDY_FILES = 64
MAX_TIDY_CHECKS = 2_048
_SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cp", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl"})
_VERSION = re.compile(r"\b(?:clang-tidy|LLVM)\s+version\s+([^\r\n]+)", re.IGNORECASE)
_CHECK_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,255}$")
_CHECK_PATTERN = re.compile(r"^[A-Za-z0-9_.*,+-]{1,1024}$")
# Greedy path matching deliberately preserves a Windows drive colon while the
# last numeric :line:column pair remains unambiguous.
_DIAGNOSTIC = re.compile(
    r"^(?P<path>.+):(?P<line>[0-9]+):(?P<column>[0-9]+):\s*"
    r"(?P<severity>warning|error|fatal error|note):\s*(?P<message>.*?)(?:\s+\[(?P<check>[^\]\r\n]+)\])?\s*$",
    re.IGNORECASE,
)


class ProcessRunner(Protocol):
    async def run(self, argv: Sequence[str], *, cwd: str = ".", timeout_seconds: float | None = None) -> ProcessResult:
        """Run one policy-bound argv process."""


@dataclass(frozen=True, slots=True)
class _ToolSelection:
    requested: str
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
        argv = [tool.requested, "--list-checks"]
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
        argv = [tool.requested, "-p", generated.relative_path]
        if pattern is not None:
            argv.append(f"--checks={pattern}")
        argv.extend(source_paths)
        try:
            result = await self._process_runtime.run(argv, cwd=".", timeout_seconds=timeout)
        except ProcessError as error:
            raise QualityToolUnavailableError("clang-tidy is not available through the configured Process Runtime.") from error
        diagnostics, omitted, parser_truncated = self._parse_diagnostics(result.stdout.text + "\n" + result.stderr.text)
        if result.timed_out:
            state = TidyExecutionState.TIMED_OUT
        elif result.exit_code != 0:
            state = TidyExecutionState.TOOL_FAILURE
        else:
            state = TidyExecutionState.COMPLETED
        return TidyRunResult(
            diagnostics=diagnostics,
            omitted_external_count=omitted,
            truncated=parser_truncated or result.stdout.truncated or result.stderr.truncated,
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
                result = await self._process_runtime.run([candidate, "--version"], cwd=".", timeout_seconds=5.0)
            except ProcessError:
                continue
            if result.timed_out or result.exit_code != 0 or result.stdout.truncated or result.stderr.truncated:
                continue
            match = _VERSION.search(result.stdout.text) or _VERSION.search(result.stderr.text)
            if match is not None:
                return _ToolSelection(candidate, canonical, match.group(1).strip()[:256])
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
        if len(set(values)) != len(values):
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

    def _parse_diagnostics(self, text: str) -> tuple[tuple[Diagnostic, ...], int, bool]:
        diagnostics: list[Diagnostic] = []
        omitted = 0
        truncated = False
        last_index: int | None = None
        for line in text.splitlines():
            match = _DIAGNOSTIC.match(line)
            if match is None:
                if last_index is not None and line and not line.lstrip().startswith(("^", "~")):
                    previous = diagnostics[last_index]
                    appended = (previous.message + " " + line.strip())[:16_384]
                    diagnostics[last_index] = previous.model_copy(update={"message": appended})
                continue
            last_index = None
            try:
                relative = self._workspace.validate_reported_path(match.group("path"))
                snapshot = self._workspace.get_snapshot(relative)
            except WorkspaceError:
                omitted += 1
                continue
            if len(diagnostics) >= MAX_TIDY_CHECKS:
                truncated = True
                continue
            severity_text = match.group("severity").casefold()
            severity = Severity.ERROR if "error" in severity_text else Severity.INFORMATION if severity_text == "note" else Severity.WARNING
            line_number = max(0, int(match.group("line")) - 1)
            column = max(0, int(match.group("column")) - 1)
            check = match.group("check")
            diagnostics.append(Diagnostic(
                message=(match.group("message").strip() or "clang-tidy diagnostic")[:16_384],
                severity=severity,
                location=Location(uri=snapshot.uri, range=Range(start=Position(line=line_number, column=column), end=Position(line=line_number, column=column))),
                code=check[:256] if check else None,
                source="clang-tidy",
            ))
            last_index = len(diagnostics) - 1
        return tuple(diagnostics), omitted, truncated
