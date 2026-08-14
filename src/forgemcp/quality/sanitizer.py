"""Read-only bounded parser for AddressSanitizer and UBSan report text."""

from __future__ import annotations

import re
from dataclasses import dataclass

from forgemcp.models import Location, Position, Range
from forgemcp.quality.errors import QualityRequestError
from forgemcp.quality.models import (
    SanitizerFinding,
    SanitizerFrame,
    SanitizerKind,
    SanitizerReportResult,
)
from forgemcp.workspace import WorkspaceError, WorkspaceService


MAX_SANITIZER_INPUT_CHARACTERS = 65_536
MAX_SANITIZER_FINDINGS = 32
MAX_SANITIZER_FRAMES = 64
_ASAN = re.compile(
    r"^(?:==[0-9]+==)?\s*(?:ERROR:\s*)?AddressSanitizer:\s*"
    r"(?P<category>[A-Za-z0-9_-]{1,128})\b",
    re.IGNORECASE,
)
_UBSAN = re.compile(
    r"^(?:==[0-9]+==)?\s*(?:UndefinedBehaviorSanitizer:\s*)?runtime error:\s*(?P<detail>[^\r\n]*)",
    re.IGNORECASE,
)
_ADDRESS = re.compile(r"\b0x[0-9A-Fa-f]+\b")
_LOCATION_SUFFIX = re.compile(
    r"(?P<path>(?:[A-Za-z]:[\\/]|/|\\\\).+?):(?P<line>[0-9]+)"
    r"(?::(?P<column>[0-9]+))?\)?$"
)
_FRAME = re.compile(
    r"^\s*#\d+\s*(?P<address>0x[0-9A-Fa-f]+)?"
    r"(?:\s+(?:in\s+)?(?P<rest>.*?))?\s*$"
)
_PATH_FRAGMENT = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\|/)")
_SOURCE_LOCATION_FRAGMENT = re.compile(
    r"\.(?:c|cc|cp|cpp|cxx|h|hh|hpp|hxx|inl):[0-9]+(?::[0-9]+)?\)?$",
    re.IGNORECASE,
)
_ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

_UBSAN_CATEGORIES = (
    ("signed integer overflow", "signed-integer-overflow"),
    ("unsigned integer overflow", "unsigned-integer-overflow"),
    ("division by zero", "division-by-zero"),
    ("null pointer", "null-pointer"),
    ("misaligned address", "misaligned-address"),
    ("shift exponent", "invalid-shift"),
    ("shift base", "invalid-shift"),
    ("out of bounds", "out-of-bounds"),
    ("type mismatch", "type-mismatch"),
    ("vptr", "invalid-vptr"),
)


@dataclass(frozen=True, slots=True)
class _Report:
    kind: SanitizerKind
    category: str
    lines: tuple[str, ...]


class SanitizerReportParser:
    """Parse supplied output only; it never launches a binary or resolves symbols."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self._workspace = workspace

    def parse(self, output: str) -> SanitizerReportResult:
        """Return bounded workspace-only findings for possibly concatenated partial reports."""
        if not isinstance(output, str):
            raise QualityRequestError("Sanitizer output must be text.")
        if len(output) > MAX_SANITIZER_INPUT_CHARACTERS:
            raise QualityRequestError("Sanitizer output exceeds the bounded parser input limit.")
        sanitized = _strip_terminal_controls(output)
        reports = self._split_reports(sanitized)
        findings: list[SanitizerFinding] = []
        omitted = 0
        truncated = len(reports) > MAX_SANITIZER_FINDINGS
        for report in reports[:MAX_SANITIZER_FINDINGS]:
            finding = self._parse_report(report)
            findings.append(finding)
            omitted += finding.omitted_external_count
            truncated = truncated or finding.truncated
        if not findings and sanitized.strip():
            findings.append(
                SanitizerFinding(
                    kind=SanitizerKind.UNKNOWN,
                    category="unrecognized",
                    summary="Unrecognized sanitizer report format.",
                    frames=(),
                    omitted_external_count=0,
                    truncated=False,
                    complete=False,
                )
            )
        return SanitizerReportResult(
            findings=tuple(findings),
            truncated=truncated,
            complete=bool(findings) and not truncated and all(item.complete for item in findings),
            omitted_external_count=omitted,
        )

    @staticmethod
    def _split_reports(output: str) -> tuple[_Report, ...]:
        reports: list[_Report] = []
        current_kind: SanitizerKind | None = None
        current_category = ""
        current_lines: list[str] = []
        for line in output.splitlines():
            asan = _ASAN.search(line)
            ubsan = _UBSAN.search(line)
            if asan is not None or ubsan is not None:
                if current_kind is not None:
                    reports.append(_Report(current_kind, current_category, tuple(current_lines)))
                if asan is not None:
                    current_kind = SanitizerKind.ADDRESS
                    current_category = asan.group("category").casefold()[:128]
                else:
                    assert ubsan is not None
                    current_kind = SanitizerKind.UNDEFINED
                    current_category = _ubsan_category(ubsan.group("detail"))
                current_lines = [line]
            elif current_kind is not None:
                current_lines.append(line)
        if current_kind is not None:
            reports.append(_Report(current_kind, current_category or "unknown", tuple(current_lines)))
        return tuple(reports)

    def _parse_report(self, report: _Report) -> SanitizerFinding:
        frames: list[SanitizerFrame] = []
        omitted = 0
        truncated = False
        complete = any("SUMMARY:" in line.upper() for line in report.lines)
        for line in report.lines:
            match = _FRAME.match(line)
            if match is None:
                if line.lstrip().startswith("#"):
                    complete = False
                continue
            if len(frames) >= MAX_SANITIZER_FRAMES:
                truncated = True
                complete = False
                break
            address = match.group("address")
            rest = (match.group("rest") or "").strip()
            function_text = rest
            location = None
            location_match = _LOCATION_SUFFIX.search(rest)
            if location_match is not None:
                function_text = rest[: location_match.start()].strip()
                try:
                    relative = self._workspace.validate_reported_path(location_match.group("path"))
                except WorkspaceError:
                    omitted += 1
                    continue
                location = self._normalise_location(
                    relative,
                    location_match.group("line"),
                    location_match.group("column") or "1",
                )
                if location is None:
                    complete = False
            function, function_truncated, external_symbol = _safe_symbol(function_text)
            if external_symbol:
                omitted += 1
                continue
            truncated = truncated or function_truncated
            frames.append(SanitizerFrame(function=function, location=location, address=address))
        summary = (
            f"AddressSanitizer reported {report.category}."
            if report.kind is SanitizerKind.ADDRESS
            else f"UndefinedBehaviorSanitizer reported {report.category}."
        )
        return SanitizerFinding(
            kind=report.kind,
            category=report.category[:256] or "unknown",
            summary=summary,
            frames=tuple(frames),
            omitted_external_count=omitted,
            truncated=truncated,
            complete=complete and not truncated,
        )

    def _normalise_location(self, relative: str, raw_line: str, raw_column: str) -> Location | None:
        """Normalize one-based UTF-8 byte coordinates without exposing source text."""
        try:
            line_value = int(raw_line)
            column_value = int(raw_column)
            if line_value < 1 or column_value < 1:
                return None
            source, snapshot = self._workspace.read_text(relative)
        except (ValueError, WorkspaceError):
            return None
        lines = _source_lines(source)
        line_index = line_value - 1
        if line_index >= len(lines):
            return None
        encoded = lines[line_index].encode("utf-8")
        byte_column = column_value - 1
        if byte_column > len(encoded):
            return None
        try:
            column = len(encoded[:byte_column].decode("utf-8"))
        except UnicodeDecodeError:
            return None
        position = Position(line=line_index, column=column)
        return Location(uri=snapshot.uri, range=Range(start=position, end=position))


def _ubsan_category(detail: str) -> str:
    lowered = detail.casefold()
    return next((category for marker, category in _UBSAN_CATEGORIES if marker in lowered), "runtime-error")


def _safe_symbol(value: str) -> tuple[str | None, bool, bool]:
    """Return a bounded symbol, hiding any path-like source or module text."""
    normalized = " ".join(value.split())
    if not normalized:
        return None, False, False
    if _PATH_FRAGMENT.search(normalized) or _SOURCE_LOCATION_FRAGMENT.search(normalized):
        return None, False, True
    truncated = len(normalized) > 1024
    return normalized[:1024], truncated, False


def _strip_terminal_controls(text: str) -> str:
    return _CONTROL.sub("", _ANSI_CSI.sub("", _ANSI_OSC.sub("", text)))


def _source_lines(source: str) -> tuple[str, ...]:
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
