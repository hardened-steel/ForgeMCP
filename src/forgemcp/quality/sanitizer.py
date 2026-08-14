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
_ASAN = re.compile(r"^(?:==[0-9]+==)?\s*(?:ERROR:\s*)?AddressSanitizer:\s*(?P<category>[^\r\n]+)", re.IGNORECASE)
_UBSAN = re.compile(r"^(?:==[0-9]+==)?\s*(?:UndefinedBehaviorSanitizer:\s*)?runtime error:\s*(?P<category>[^\r\n]+)", re.IGNORECASE)
_ADDRESS = re.compile(r"\b0x[0-9A-Fa-f]+\b")
_LOCATION_SUFFIX = re.compile(r"(?P<path>(?:[A-Za-z]:[\\/]|/).+):(?P<line>[0-9]+)(?::(?P<column>[0-9]+))?$")
_FRAME = re.compile(r"^\s*#\d+\s*(?P<address>0x[0-9A-Fa-f]+)?(?:\s+in\s+(?P<rest>.*))?$")


@dataclass(frozen=True, slots=True)
class _Report:
    kind: SanitizerKind
    category: str
    lines: tuple[str, ...]


class SanitizerReportParser:
    """Parse supplied output only; it never launches an instrumented binary or downloads symbols."""

    def __init__(self, workspace: WorkspaceService) -> None:
        self._workspace = workspace

    def parse(self, output: str) -> SanitizerReportResult:
        """Return bounded workspace-only findings for possibly concatenated partial reports."""
        if not isinstance(output, str):
            raise QualityRequestError("Sanitizer output must be text.")
        if len(output) > MAX_SANITIZER_INPUT_CHARACTERS:
            raise QualityRequestError("Sanitizer output exceeds the bounded parser input limit.")
        reports, truncated = self._split_reports(output)
        findings: list[SanitizerFinding] = []
        omitted = 0
        for report in reports[:MAX_SANITIZER_FINDINGS]:
            finding = self._parse_report(report)
            findings.append(finding)
            omitted += finding.omitted_external_count
        if len(reports) > MAX_SANITIZER_FINDINGS:
            truncated = True
        if not findings and output.strip():
            findings.append(
                SanitizerFinding(
                    kind=SanitizerKind.UNKNOWN,
                    category="unrecognized",
                    summary="Unrecognized sanitizer report format.",
                    frames=(),
                    omitted_external_count=0,
                    complete=False,
                )
            )
        return SanitizerReportResult(
            findings=tuple(findings),
            truncated=truncated,
            complete=bool(findings) and all(item.complete for item in findings),
            omitted_external_count=omitted,
        )

    @staticmethod
    def _split_reports(output: str) -> tuple[tuple[_Report, ...], bool]:
        reports: list[_Report] = []
        current_kind: SanitizerKind | None = None
        current_category = ""
        current_lines: list[str] = []
        for line in output.splitlines():
            asan = _ASAN.search(line)
            ubsan = _UBSAN.search(line)
            match = asan or ubsan
            if match is not None:
                if current_kind is not None:
                    reports.append(_Report(current_kind, current_category, tuple(current_lines)))
                current_kind = SanitizerKind.ADDRESS if asan is not None else SanitizerKind.UNDEFINED
                current_category = _clean_summary(match.group("category"), 256)
                current_lines = [line]
            elif current_kind is not None:
                current_lines.append(line)
        if current_kind is not None:
            reports.append(_Report(current_kind, current_category or "unknown", tuple(current_lines)))
        return tuple(reports), False

    def _parse_report(self, report: _Report) -> SanitizerFinding:
        frames: list[SanitizerFrame] = []
        omitted = 0
        complete = any("SUMMARY:" in line.upper() for line in report.lines)
        for line in report.lines:
            match = _FRAME.match(line)
            if match is None:
                continue
            if len(frames) >= MAX_SANITIZER_FRAMES:
                complete = False
                break
            address = match.group("address")
            rest = (match.group("rest") or "").strip()
            function: str | None = rest[:1024] or None
            location = None
            location_match = _LOCATION_SUFFIX.search(rest)
            if location_match is not None:
                function = rest[: location_match.start()].strip()[:1024] or None
                try:
                    relative = self._workspace.validate_reported_path(location_match.group("path"))
                    snapshot = self._workspace.get_snapshot(relative)
                    line_number = max(0, int(location_match.group("line")) - 1)
                    column = max(0, int(location_match.group("column") or "1") - 1)
                    location = Location(
                        uri=snapshot.uri,
                        range=Range(start=Position(line=line_number, column=column), end=Position(line=line_number, column=column)),
                    )
                except WorkspaceError:
                    omitted += 1
                    continue
            frames.append(SanitizerFrame(function=function, location=location, address=address))
        summary = _clean_summary(report.category, 4096)
        return SanitizerFinding(
            kind=report.kind,
            category=report.category[:256] or "unknown",
            summary=summary or "Sanitizer report.",
            frames=tuple(frames),
            omitted_external_count=omitted,
            complete=complete,
        )


def _clean_summary(value: str, limit: int) -> str:
    """Keep report summaries bounded while retaining addresses only as opaque frame data."""
    return _ADDRESS.sub("<address>", " ".join(value.split()))[:limit]
