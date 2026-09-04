"""Builtin QualityPlugin exposing only fixed, transport-neutral ToolContributions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from time import monotonic

from pydantic import Field, ValidationError

from forgemcp.core.errors import ForgeMCPError, to_mcp_error_response
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import (
    AppCsp,
    AppResourceContribution,
    ForgePlugin,
    NoOpProgressReporter,
    PluginContext,
    PluginMetadata,
    ProgressUpdate,
    ToolContribution,
    ToolAppBinding,
    ToolExecutionContext,
)
from forgemcp.project import ComponentState, ComponentStatus, ProjectStatusRegistry, StatusFact
from forgemcp.project.models import utc_now
from forgemcp.processes import ProcessRuntime
from forgemcp.quality.clang_format import ClangFormatService
from forgemcp.quality.clang_tidy import ClangTidyService
from forgemcp.quality.errors import QualityRequestError
from forgemcp.quality.models import QualityStatus
from forgemcp.quality.sanitizer import MAX_SANITIZER_INPUT_CHARACTERS, SanitizerReportParser
from forgemcp.workspace import WorkspaceService
from forgemcp.toolchain import ToolchainDiscoveryService


QUALITY_OVERVIEW_APP_URI = "ui://forgemcp/quality/overview"
QUALITY_FINDINGS_APP_URI = "ui://forgemcp/quality/findings"


class _EmptyArguments(ForgeModel):
    """No arguments are accepted by this status tool."""


class _FormatCheckArguments(ForgeModel):
    paths: list[str] = Field(min_length=1, max_length=64, description="Explicit workspace-relative C/C++ source files; globs and recursive selection are unavailable.")


class _FormatApplyFile(ForgeModel):
    path: str = Field(min_length=1, max_length=4096, description="Explicit workspace-relative C/C++ source file.")
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", description="Required SHA-256 snapshot captured before this mutation request.")


class _FormatApplyArguments(ForgeModel):
    files: list[_FormatApplyFile] = Field(min_length=1, max_length=64, description="Every file must carry a required snapshot SHA-256; no unconditional writes exist.")


class _TidyChecksArguments(ForgeModel):
    checks: str | None = Field(default=None, max_length=1024, description="Optional bounded clang-tidy check pattern, never command-line arguments.")


class _TidyRunArguments(ForgeModel):
    paths: list[str] = Field(min_length=1, max_length=64, description="Explicit workspace-relative C/C++ source files.")
    compile_commands_dir: str = Field(min_length=1, max_length=4096, description="Validated workspace-generated directory containing compile_commands.json.")
    checks: str | None = Field(default=None, max_length=1024, description="Optional bounded clang-tidy check pattern, never flags or arbitrary config.")
    timeout_seconds: float | None = Field(default=None, gt=0, le=300, description="Optional bounded clang-tidy execution timeout in seconds.")


class _SanitizerArguments(ForgeModel):
    output: str = Field(max_length=MAX_SANITIZER_INPUT_CHARACTERS, description="Bounded supplied sanitizer output parsed read-only; it is never executed or logged.")


ToolOperation = Callable[..., Awaitable[ForgeModel]]


@dataclass(frozen=True, slots=True)
class _QualityOperationCache:
    operation: str
    outcome: str
    item_count: int
    duration_milliseconds: int
    observed_at: datetime


class _QualityStatusProvider:
    id = "quality"

    def __init__(self, plugin: "QualityPlugin") -> None:
        self._plugin = plugin

    async def snapshot_status(self) -> ComponentStatus:
        format_info = self._plugin._format_info or self._plugin.clang_format.cached_status
        tidy_info = self._plugin._tidy_info or self._plugin.clang_tidy.cached_status
        state = ComponentState.ACTIVE if self._plugin._active_operations else ComponentState.IDLE
        if (
            format_info is not None
            and tidy_info is not None
            and not format_info.available
            and not tidy_info.available
        ):
            state = ComponentState.UNAVAILABLE
        required_unavailable = (
            self._plugin._format_required
            and format_info is not None
            and not format_info.available
        ) or (
            self._plugin._tidy_required
            and tidy_info is not None
            and not tidy_info.available
        )
        if required_unavailable:
            state = ComponentState.DEGRADED
        facts = [
            StatusFact(name="clang_format_observed", value=format_info is not None),
            StatusFact(name="clang_format_available", value=format_info.available if format_info else False),
            StatusFact(name="clang_tidy_observed", value=tidy_info is not None),
            StatusFact(name="clang_tidy_available", value=tidy_info.available if tidy_info else False),
            StatusFact(name="active_operations", value=self._plugin._active_operations),
            StatusFact(name="sanitizer_parser_count", value=3),
        ]
        if format_info is not None and format_info.version is not None:
            facts.append(StatusFact(name="clang_format_version", value=format_info.version))
        if tidy_info is not None and tidy_info.version is not None:
            facts.append(StatusFact(name="clang_tidy_version", value=tidy_info.version))
        warnings: list[str] = []
        for item in (self._plugin._last_format, self._plugin._last_tidy):
            if item is None:
                continue
            prefix = f"last_{item.operation}"
            facts.extend(
                (
                    StatusFact(name=f"{prefix}_outcome", value=item.outcome),
                    StatusFact(name=f"{prefix}_item_count", value=item.item_count),
                    StatusFact(name=f"{prefix}_duration", value=item.duration_milliseconds, unit="milliseconds"),
                    StatusFact(name=f"{prefix}_observed_at", value=item.observed_at.isoformat()),
                )
            )
            if item.outcome != "success":
                warnings.append(f"{prefix}_{item.outcome}")
        if format_info is None or tidy_info is None:
            warnings.append("tool_availability_not_observed")
        if required_unavailable:
            warnings.append("required_capability_unavailable")
        return ComponentStatus(
            id=self.id,
            display_name="Quality Tools",
            state=state,
            capabilities=("clang-format", "clang-tidy", "sanitizer-report"),
            summary="Cached quality-tool qualification and last-operation metadata.",
            facts=tuple(facts),
            warnings=tuple(warnings),
            observed_at=utc_now(),
        )


class QualityPlugin(ForgePlugin):
    """Builtin, non-persistent quality capability for formatting, analysis, and report parsing."""

    __slots__ = (
        "_format", "_tidy", "_sanitizer", "_status_registry", "_active_operations",
        "_format_info", "_tidy_info", "_last_format", "_last_tidy",
        "_format_required", "_tidy_required",
    )

    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                plugin_id="quality",
                requires_services=("workspace", "process_runtime", "toolchain_discovery", "project_status_registry"),
                provides=frozenset({"clang-format", "clang-tidy", "sanitizer-report"}),
                tool_namespaces=("clang_format", "clang_tidy", "sanitizer"),
            )
        )
        self._format: ClangFormatService | None = None
        self._tidy: ClangTidyService | None = None
        self._sanitizer: SanitizerReportParser | None = None
        self._status_registry: ProjectStatusRegistry | None = None
        self._active_operations = 0
        self._format_info = None
        self._tidy_info = None
        self._last_format: _QualityOperationCache | None = None
        self._last_tidy: _QualityOperationCache | None = None
        self._format_required = False
        self._tidy_required = False

    @property
    def clang_format(self) -> ClangFormatService:
        if self._format is None:
            raise RuntimeError("The Quality plugin is not running.")
        return self._format

    @property
    def clang_tidy(self) -> ClangTidyService:
        if self._tidy is None:
            raise RuntimeError("The Quality plugin is not running.")
        return self._tidy

    @property
    def sanitizer(self) -> SanitizerReportParser:
        if self._sanitizer is None:
            raise RuntimeError("The Quality plugin is not running.")
        return self._sanitizer

    async def start(self, context: PluginContext) -> None:
        workspace = context.services.get("workspace")
        runtime = context.services.get("process_runtime")
        toolchain = context.services.get("toolchain_discovery")
        status_registry = context.services.get("project_status_registry")
        if not isinstance(workspace, WorkspaceService) or not isinstance(runtime, ProcessRuntime):
            raise TypeError("The Quality plugin requires WorkspaceService and ProcessRuntime.")
        if not isinstance(status_registry, ProjectStatusRegistry):
            raise TypeError("The Quality plugin requires ProjectStatusRegistry.")
        if not isinstance(toolchain, ToolchainDiscoveryService):
            raise TypeError("The Quality plugin requires ToolchainDiscoveryService.")
        self._format = ClangFormatService(context.config, workspace, runtime, toolchain)
        self._tidy = ClangTidyService(context.config, workspace, runtime, toolchain)
        self._sanitizer = SanitizerReportParser(workspace)
        self._format_required = context.config.clang_format_path is not None
        self._tidy_required = context.config.clang_tidy_path is not None
        self._status_registry = status_registry
        status_registry.register(_QualityStatusProvider(self))
        self._register_tools(context)
        self._register_apps(context)

    async def stop(self) -> None:
        """Release only in-memory service references; no persistent process is owned."""
        if self._status_registry is not None:
            self._status_registry.unregister("quality")
            self._status_registry = None
        self._sanitizer = None
        self._tidy = None
        self._format = None

    def _register_tools(self, context: PluginContext) -> None:
        contributions = (
            ("quality", "status", "Report fixed clang-format/clang-tidy availability and parser scope.", _EmptyArguments, QualityPlugin._status),
            ("clang_format", "check", "Check explicitly named workspace C/C++ files with project clang-format rules without modifying them.", _FormatCheckArguments, QualityPlugin._check),
            ("clang_format", "apply", "Apply one verified snapshot-CAS clang-format batch only when every required SHA-256 still matches.", _FormatApplyArguments, QualityPlugin._apply),
            ("clang_tidy", "list_checks", "List a bounded sorted clang-tidy check set without exposing arbitrary arguments.", _TidyChecksArguments, QualityPlugin._list_checks),
            ("clang_tidy", "run", "Run fixed read-only clang-tidy analysis using an explicit generated compile_commands directory; fixes are unavailable.", _TidyRunArguments, QualityPlugin._run_tidy),
            ("sanitizer", "parse_report", "Parse bounded supplied ASan/UBSan output read-only; this tool never launches a binary.", _SanitizerArguments, QualityPlugin._parse_report),
        )
        for namespace, name, description, model, operation in contributions:
            context.tools.register(ToolContribution(
                name=name,
                description=description,
                input_model=model,
                namespace=namespace,
                handler=lambda arguments, m=model, op=operation, *, execution_context=None: self._dispatch(m, arguments, op, execution_context),
            ))

    @staticmethod
    def _register_apps(context: PluginContext) -> None:
        """Register immutable Quality App assets without changing tool behavior."""
        try:
            overview = files("forgemcp.apps.assets").joinpath("quality-overview.html").read_text(encoding="utf-8")
            findings = files("forgemcp.apps.assets").joinpath("quality-findings.html").read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError, OSError, UnicodeError) as error:
            raise RuntimeError("Quality App asset is unavailable.") from error
        csp = AppCsp(connect_domains=(), resource_domains=(), frame_domains=(), base_uri_domains=())
        context.apps.register_resource(AppResourceContribution(
            uri=QUALITY_OVERVIEW_APP_URI,
            name="forgemcp_quality_overview_app",
            description="Interactive read-only Quality tool and format overview.",
            html=overview,
            csp=csp,
            prefers_border=True,
        ))
        context.apps.register_resource(AppResourceContribution(
            uri=QUALITY_FINDINGS_APP_URI,
            name="forgemcp_quality_findings_app",
            description="Interactive read-only clang-tidy and sanitizer findings view.",
            html=findings,
            csp=csp,
            prefers_border=True,
        ))
        for tool_name in (
            "quality__status",
            "clang_format__check",
            "clang_format__apply",
            "clang_tidy__list_checks",
        ):
            context.apps.bind_tool(ToolAppBinding(
                tool_name=tool_name,
                resource_uri=QUALITY_OVERVIEW_APP_URI,
                visibility=("model", "app"),
            ))
        for tool_name in ("clang_tidy__run", "sanitizer__parse_report"):
            context.apps.bind_tool(ToolAppBinding(
                tool_name=tool_name,
                resource_uri=QUALITY_FINDINGS_APP_URI,
                visibility=("model", "app"),
            ))

    async def _dispatch(
        self,
        model: type[ForgeModel],
        arguments: Mapping[str, object],
        operation: ToolOperation,
        execution_context: ToolExecutionContext | None = None,
    ) -> dict[str, object]:
        try:
            request = model.model_validate(arguments)
        except ValidationError:
            return to_mcp_error_response(QualityRequestError("Tool arguments do not match the published Quality schema.")).as_dict()
        try:
            result = await operation(self, request, execution_context or ToolExecutionContext(NoOpProgressReporter()))
        except ForgeMCPError as error:
            return to_mcp_error_response(error).as_dict()
        return result.model_dump(mode="json")

    async def _status(self, _: ForgeModel, __: ToolExecutionContext) -> QualityStatus:
        self._active_operations += 1
        try:
            format_info = await self.clang_format.status()
            tidy_info = await self.clang_tidy.status()
            self._format_info = format_info
            self._tidy_info = tidy_info
        finally:
            self._active_operations -= 1
        return QualityStatus(
            clang_format=format_info,
            clang_tidy=tidy_info,
            sanitizer_parsers=("address_sanitizer", "undefined_behavior_sanitizer", "unknown"),
            platform_limitations=(
                "Quality tools are qualified lazily; missing executables do not prevent startup.",
                "compile_commands availability is evaluated only for each clang-tidy request directory.",
                "clang-format uses bounded replacement XML and never invokes -i.",
                "Project format/tidy configuration and compilation databases are trusted inputs; ForgeMCP is not a sandbox.",
                "Phase 1 parses sanitizer output only and never runs instrumented binaries.",
            ),
        )

    async def _check(self, request: ForgeModel, __: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _FormatCheckArguments)
        started = monotonic()
        self._active_operations += 1
        try:
            result = await self.clang_format.check(request.paths)
        except asyncio.CancelledError:
            self._last_format = self._operation_cache("format", "cancelled", len(request.paths), started)
            raise
        except Exception:
            self._last_format = self._operation_cache("format", "failure", len(request.paths), started)
            raise
        finally:
            self._active_operations -= 1
        self._last_format = self._operation_cache(
            "format", "success" if all(item.error is None for item in result.files) else "failure",
            len(result.files), started
        )
        return result

    async def _apply(self, request: ForgeModel, __: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _FormatApplyArguments)
        started = monotonic()
        self._active_operations += 1
        try:
            result = await self.clang_format.apply(tuple((item.path, item.expected_sha256) for item in request.files))
        except asyncio.CancelledError:
            self._last_format = self._operation_cache("format", "cancelled", len(request.files), started)
            raise
        except Exception:
            self._last_format = self._operation_cache("format", "failure", len(request.files), started)
            raise
        finally:
            self._active_operations -= 1
        self._last_format = self._operation_cache(
            "format", "success" if result.applied else "failure", len(result.files), started
        )
        return result

    async def _list_checks(self, request: ForgeModel, __: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _TidyChecksArguments)
        started = monotonic()
        self._active_operations += 1
        try:
            result = await self.clang_tidy.list_checks(request.checks)
        except asyncio.CancelledError:
            self._last_tidy = self._operation_cache("tidy", "cancelled", 0, started)
            raise
        except Exception:
            self._last_tidy = self._operation_cache("tidy", "failure", 0, started)
            raise
        finally:
            self._active_operations -= 1
        outcome = "success" if result.process.exit_code == 0 and not result.process.timed_out else "failure"
        self._last_tidy = self._operation_cache("tidy", outcome, len(result.checks), started)
        return result

    async def _run_tidy(self, request: ForgeModel, execution_context: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _TidyRunArguments)
        started = monotonic()
        self._active_operations += 1
        heartbeat: asyncio.Task[None] | None = None
        try:
            await execution_context.report_progress(ProgressUpdate(0, None, "Preparing clang-tidy analysis"))
            if execution_context.supports_progress:
                heartbeat = asyncio.create_task(self._progress_heartbeat(execution_context, started))
            await execution_context.report_progress(ProgressUpdate(1, None, "clang-tidy analysis started"))
            result = await self.clang_tidy.run(
                paths=request.paths,
                compile_commands_dir=request.compile_commands_dir,
                checks=request.checks,
                timeout_seconds=request.timeout_seconds,
            )
        except asyncio.CancelledError:
            self._last_tidy = self._operation_cache("tidy", "cancelled", len(request.paths), started)
            await execution_context.report_progress(ProgressUpdate(1, None, "clang-tidy analysis cancelled", terminal=True))
            raise
        except Exception:
            self._last_tidy = self._operation_cache("tidy", "failure", len(request.paths), started)
            await execution_context.report_progress(ProgressUpdate(1, None, "clang-tidy analysis failed", terminal=True))
            raise
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            self._active_operations -= 1
        outcome = "success" if result.execution_state.value == "completed" else "failure"
        self._last_tidy = self._operation_cache("tidy", outcome, len(result.diagnostics), started)
        message = "clang-tidy analysis completed" if outcome == "success" else "clang-tidy analysis failed"
        await execution_context.report_progress(
            ProgressUpdate(2, None, message, terminal=True, completed=outcome == "success")
        )
        return result

    @staticmethod
    def _operation_cache(
        operation: str,
        outcome: str,
        item_count: int,
        started: float,
    ) -> _QualityOperationCache:
        return _QualityOperationCache(
            operation=operation,
            outcome=outcome,
            item_count=max(0, item_count),
            duration_milliseconds=max(0, int((monotonic() - started) * 1000)),
            observed_at=datetime.now(UTC),
        )

    async def _parse_report(self, request: ForgeModel, __: ToolExecutionContext) -> ForgeModel:
        assert isinstance(request, _SanitizerArguments)
        return self.sanitizer.parse(request.output)

    @staticmethod
    async def _progress_heartbeat(context: ToolExecutionContext, started: float) -> None:
        while True:
            await asyncio.sleep(2.0)
            await context.report_progress(
                ProgressUpdate(1, None, f"clang-tidy analysis running ({max(1, int(monotonic() - started))}s)")
            )
