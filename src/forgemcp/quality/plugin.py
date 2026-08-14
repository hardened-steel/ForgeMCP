"""Builtin QualityPlugin exposing only fixed, transport-neutral ToolContributions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from pydantic import Field, ValidationError

from forgemcp.core.errors import ForgeMCPError, to_mcp_error_response
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import ForgePlugin, PluginContext, PluginMetadata, ToolContribution
from forgemcp.processes import ProcessRuntime
from forgemcp.quality.clang_format import ClangFormatService
from forgemcp.quality.clang_tidy import ClangTidyService
from forgemcp.quality.errors import QualityRequestError
from forgemcp.quality.models import QualityStatus
from forgemcp.quality.sanitizer import MAX_SANITIZER_INPUT_CHARACTERS, SanitizerReportParser
from forgemcp.workspace import WorkspaceService


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


ToolOperation = Callable[["QualityPlugin", ForgeModel], Awaitable[ForgeModel]]


class QualityPlugin(ForgePlugin):
    """Builtin, non-persistent quality capability for formatting, analysis, and report parsing."""

    __slots__ = ("_format", "_tidy", "_sanitizer")

    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                plugin_id="quality",
                requires_services=("workspace", "process_runtime"),
                provides=frozenset({"clang-format", "clang-tidy", "sanitizer-report"}),
                tool_namespaces=("clang_format", "clang_tidy", "sanitizer"),
            )
        )
        self._format: ClangFormatService | None = None
        self._tidy: ClangTidyService | None = None
        self._sanitizer: SanitizerReportParser | None = None

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
        if not isinstance(workspace, WorkspaceService) or not isinstance(runtime, ProcessRuntime):
            raise TypeError("The Quality plugin requires WorkspaceService and ProcessRuntime.")
        self._format = ClangFormatService(context.config, workspace, runtime)
        self._tidy = ClangTidyService(context.config, workspace, runtime)
        self._sanitizer = SanitizerReportParser(workspace)
        self._register_tools(context)

    async def stop(self) -> None:
        """Release only in-memory service references; no persistent process is owned."""
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
                handler=lambda arguments, m=model, op=operation: self._dispatch(m, arguments, op),
            ))

    async def _dispatch(self, model: type[ForgeModel], arguments: Mapping[str, object], operation: ToolOperation) -> dict[str, object]:
        try:
            request = model.model_validate(arguments)
        except ValidationError:
            return to_mcp_error_response(QualityRequestError("Tool arguments do not match the published Quality schema.")).as_dict()
        try:
            result = await operation(self, request)
        except ForgeMCPError as error:
            return to_mcp_error_response(error).as_dict()
        return result.model_dump(mode="json")

    async def _status(self, _: ForgeModel) -> QualityStatus:
        return QualityStatus(
            clang_format=await self.clang_format.status(),
            clang_tidy=await self.clang_tidy.status(),
            sanitizer_parsers=("address_sanitizer", "undefined_behavior_sanitizer", "unknown"),
            platform_limitations=(
                "Quality tools are qualified lazily; missing executables do not prevent startup.",
                "compile_commands availability is evaluated only for each clang-tidy request directory.",
                "clang-format uses bounded replacement XML and never invokes -i.",
                "Project format/tidy configuration and compilation databases are trusted inputs; ForgeMCP is not a sandbox.",
                "Phase 1 parses sanitizer output only and never runs instrumented binaries.",
            ),
        )

    async def _check(self, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _FormatCheckArguments)
        return await self.clang_format.check(request.paths)

    async def _apply(self, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _FormatApplyArguments)
        return await self.clang_format.apply(tuple((item.path, item.expected_sha256) for item in request.files))

    async def _list_checks(self, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _TidyChecksArguments)
        return await self.clang_tidy.list_checks(request.checks)

    async def _run_tidy(self, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _TidyRunArguments)
        return await self.clang_tidy.run(
            paths=request.paths,
            compile_commands_dir=request.compile_commands_dir,
            checks=request.checks,
            timeout_seconds=request.timeout_seconds,
        )

    async def _parse_report(self, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _SanitizerArguments)
        return self.sanitizer.parse(request.output)
