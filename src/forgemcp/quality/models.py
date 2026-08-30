"""Immutable, transport-neutral models for the bounded Quality feature."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from forgemcp.models import Diagnostic, Location
from forgemcp.models._base import ForgeModel


class QualityProcessSummary(ForgeModel):
    """Safe completion facts for a quality-tool invocation, without raw output."""

    exit_code: int | None = Field(default=None, description="Exit code when the process completed normally.")
    timed_out: bool = Field(default=False, description="Whether the process exceeded its bounded timeout.")
    stdout_truncated: bool = Field(default=False, description="Whether bounded standard-output capture lost data.")
    stderr_truncated: bool = Field(default=False, description="Whether bounded standard-error capture lost data.")


class QualityToolInfo(ForgeModel):
    """Qualified availability information for a fixed local quality executable."""

    executable: str | None = Field(default=None, max_length=64, description="Stable public tool identity when qualification succeeded, never its resolved path.")
    available: bool = Field(description="Whether the fixed executable produced a parseable version banner.")
    version: str | None = Field(default=None, min_length=1, max_length=256, description="Parsed local tool version when available.")
    error: str | None = Field(default=None, min_length=1, max_length=512, description="Intentional safe reason when qualification is unavailable.")

    @model_validator(mode="after")
    def availability_matches_metadata(self) -> "QualityToolInfo":
        if self.available and (self.executable is None or self.version is None or self.error is not None):
            raise ValueError("Available quality tools require executable and version without an error.")
        if not self.available and self.version is not None:
            raise ValueError("Unavailable quality tools must not expose a version.")
        return self


class QualityStatus(ForgeModel):
    """Feature status; build-directory state is intentionally not global here."""

    clang_format: QualityToolInfo = Field(description="Local clang-format qualification status.")
    clang_tidy: QualityToolInfo = Field(description="Local clang-tidy qualification status.")
    sanitizer_parsers: tuple[str, ...] = Field(max_length=8, description="Read-only sanitizer report parser kinds supported by this server.")
    platform_limitations: tuple[str, ...] = Field(max_length=16, description="Intentional current-platform and Phase 1 limitations.")


class FormatFileResult(ForgeModel):
    """Content-free check result for one explicitly requested C or C++ source file."""

    path: str = Field(min_length=1, max_length=4096, description="Validated workspace-relative source path.")
    snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", description="SHA-256 of the source snapshot used for formatting when it was read.")
    would_change: bool | None = Field(default=None, description="Whether the verified formatted form differs; null on per-file failure.")
    formatted_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", description="SHA-256 of the verified formatted result, never its source text.")
    process: QualityProcessSummary | None = Field(default=None, description="Safe process completion summary when clang-format ran.")
    error: str | None = Field(default=None, min_length=1, max_length=512, description="Intentional safe per-file failure description.")

    @model_validator(mode="after")
    def completed_result_has_hashes(self) -> "FormatFileResult":
        if self.error is None and (self.snapshot_sha256 is None or self.would_change is None or self.formatted_sha256 is None):
            raise ValueError("Successful format results require snapshot, comparison, and formatted digest.")
        return self


class FormatCheckResult(ForgeModel):
    """Bounded read-only batch result for clang-format checks."""

    files: tuple[FormatFileResult, ...] = Field(max_length=64, description="One independent structured result for each requested file.")
    clean: bool = Field(description="True only when every requested file completed and would not change.")


class FormatApplyResult(ForgeModel):
    """Result of one snapshot-guarded, multi-file formatting commit."""

    applied: bool = Field(description="Whether all requested formatting edits were committed together.")
    files: tuple[FormatFileResult, ...] = Field(max_length=64, description="Pre-commit formatting results for the complete request.")
    conflict: bool = Field(default=False, description="Whether compare-and-swap rejected the batch without changing files.")


class TidyCheckList(ForgeModel):
    """A bounded sorted clang-tidy check-name listing."""

    checks: tuple[str, ...] = Field(max_length=2048, description="Sorted enabled check names after the optional fixed pattern filter.")
    truncated: bool = Field(description="Whether the bounded result omitted additional check names.")
    process: QualityProcessSummary = Field(description="Safe clang-tidy listing process outcome.")


class TidyExecutionState(StrEnum):
    """Distinguish findings from tool failure and timeout without raw output."""

    COMPLETED = "completed"
    TOOL_FAILURE = "tool_failure"
    TIMED_OUT = "timed_out"


class TidyRunResult(ForgeModel):
    """Normalized clang-tidy diagnostics with external locations omitted."""

    diagnostics: tuple[Diagnostic, ...] = Field(max_length=2048, description="Workspace-only normalized compiler-style clang-tidy diagnostics.")
    omitted_external_count: int = Field(ge=0, description="Diagnostics for non-workspace files intentionally omitted.")
    omitted_invalid_count: int = Field(ge=0, description="Diagnostic-looking records omitted because their location or syntax was not safely normalizable.")
    truncated: bool = Field(description="Whether process capture or the diagnostics collection was bounded.")
    complete: bool = Field(description="Whether all captured diagnostic-looking records were normalized without truncation.")
    execution_state: TidyExecutionState = Field(description="Completed analysis, failed tool invocation, or timeout.")
    process: QualityProcessSummary = Field(description="Safe process completion facts without raw diagnostics output.")


class SanitizerKind(StrEnum):
    """The bounded report families recognized by the Phase 1 read-only parser."""

    ADDRESS = "address_sanitizer"
    UNDEFINED = "undefined_behavior_sanitizer"
    UNKNOWN = "unknown"


class SanitizerFrame(ForgeModel):
    """One bounded sanitizer stack frame with no external source disclosure."""

    function: str | None = Field(default=None, min_length=1, max_length=1024, description="Symbol text reported by the sanitizer, when present.")
    location: Location | None = Field(default=None, description="Workspace-only normalized frame location when safely validated.")
    address: str | None = Field(default=None, min_length=1, max_length=256, description="Opaque reported address; it is never dereferenced or resolved.")


class SanitizerFinding(ForgeModel):
    """One parsed sanitizer finding without raw report text or source content."""

    kind: SanitizerKind = Field(description="Detected sanitizer family or safe unknown fallback.")
    category: str = Field(min_length=1, max_length=256, description="Bounded sanitizer error category.")
    summary: str = Field(min_length=1, max_length=4096, description="Bounded human-facing summary stripped of addresses and raw source content.")
    frames: tuple[SanitizerFrame, ...] = Field(max_length=64, description="Bounded workspace-safe stack frames.")
    omitted_external_count: int = Field(ge=0, description="External stack frames intentionally hidden from the response.")
    truncated: bool = Field(description="Whether frame or string bounds omitted part of this finding.")
    complete: bool = Field(description="Whether this finding had a recognizable complete header and termination.")


class SanitizerReportResult(ForgeModel):
    """Read-only parse result for one bounded sanitizer-output input."""

    findings: tuple[SanitizerFinding, ...] = Field(max_length=32, description="Recognized reports, including safe unknown fallback findings.")
    truncated: bool = Field(description="Whether input or bounded parsing omitted report data.")
    complete: bool = Field(description="Whether every returned finding was parsed to a recognizable end.")
    omitted_external_count: int = Field(ge=0, description="Total external stack frames omitted from all findings.")
