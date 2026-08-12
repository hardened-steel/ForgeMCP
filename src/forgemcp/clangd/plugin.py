"""Builtin clangd feature plugin and its ToolContribution-only MCP surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from pydantic import Field, ValidationError

from forgemcp.clangd.errors import ClangdRequestError
from forgemcp.clangd.service import ClangdService
from forgemcp.core.errors import ForgeMCPError, to_mcp_error_response
from forgemcp.models import Position
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import ForgePlugin, PluginContext, PluginMetadata, ToolContribution
from forgemcp.processes import ProcessRuntime
from forgemcp.workspace import WorkspaceService


class _StatusArguments(ForgeModel):
    """clangd status deliberately accepts no options."""


class _StartArguments(ForgeModel):
    compile_commands_dir: str = Field(description="Workspace-relative directory containing compile_commands.json.")


class _StopArguments(ForgeModel):
    """clangd stop deliberately accepts no options."""


class _DocumentArguments(ForgeModel):
    path: str = Field(description="Workspace-relative UTF-8 source file path.")


class _DiagnosticsArguments(_DocumentArguments):
    timeout_seconds: float | None = Field(default=None, description="Bounded wait for current publishDiagnostics.")


class _PositionArguments(_DocumentArguments):
    position: Position = Field(description="Zero-based line and Unicode code-point column.")


class _ReferencesArguments(_PositionArguments):
    include_declaration: bool = Field(default=False, description="Whether the declaration is included among references.")


class _WorkspaceSymbolsArguments(ForgeModel):
    query: str = Field(description="Workspace-symbol text query.")
    limit: int | None = Field(default=None, description="Optional bounded result count from 1 through 500.")


ToolOperation = Callable[[ClangdService, ForgeModel], Awaitable[ForgeModel | None]]


class ClangdPlugin(ForgePlugin):
    """Application-scoped clangd plugin; it creates no server until ``start`` tool use."""

    __slots__ = ("_service",)

    def __init__(self) -> None:
        super().__init__(
            PluginMetadata(
                plugin_id="clangd",
                requires_services=("workspace", "process_runtime"),
                provides=frozenset({"clangd"}),
            )
        )
        self._service: ClangdService | None = None

    @property
    def service(self) -> ClangdService:
        """Return the current application's service while this plugin is running."""
        if self._service is None:
            raise RuntimeError("The clangd plugin is not running.")
        return self._service

    async def start(self, context: PluginContext) -> None:
        workspace = context.services.get("workspace")
        process_runtime = context.services.get("process_runtime")
        if not isinstance(workspace, WorkspaceService) or not isinstance(process_runtime, ProcessRuntime):
            raise TypeError("The clangd plugin requires WorkspaceService and ProcessRuntime.")
        self._service = ClangdService(context.config, workspace, process_runtime)
        self._register_tools(context)

    async def stop(self) -> None:
        """Stop the managed server before Process Runtime closes its child handles."""
        if self._service is not None:
            await self._service.aclose()
            self._service = None

    def _register_tools(self, context: PluginContext) -> None:
        contributions = (
            ("status", "Report safe clangd discovery and session status.", _StatusArguments, self._status),
            ("start", "Start and initialize clangd for an explicit compilation database directory.", _StartArguments, self._start),
            ("stop", "Gracefully stop the managed clangd session.", _StopArguments, self._stop),
            ("diagnostics", "Synchronize a source file and return diagnostics for its current snapshot.", _DiagnosticsArguments, self._diagnostics),
            ("hover", "Return normalized hover information at a source position.", _PositionArguments, self._hover),
            ("definition", "Return bounded workspace-contained definition locations.", _PositionArguments, self._definition),
            ("references", "Return bounded workspace-contained reference locations.", _ReferencesArguments, self._references),
            ("document_symbols", "Return bounded hierarchical symbols for one source document.", _DocumentArguments, self._document_symbols),
            ("workspace_symbols", "Return bounded workspace-contained symbol matches.", _WorkspaceSymbolsArguments, self._workspace_symbols),
        )
        for name, description, model, operation in contributions:
            context.tools.register(
                ToolContribution(
                    name=name,
                    description=description,
                    input_model=model,
                    handler=lambda arguments, m=model, op=operation: self._dispatch(m, arguments, op),
                )
            )

    async def _dispatch(
        self,
        model_type: type[ForgeModel],
        arguments: Mapping[str, object],
        operation: ToolOperation,
    ) -> dict[str, object]:
        try:
            request = model_type.model_validate(arguments)
        except ValidationError:
            return to_mcp_error_response(
                ClangdRequestError("Tool arguments do not match the published clangd schema.")
            ).as_dict()
        try:
            result = await operation(self.service, request)
        except ForgeMCPError as error:
            return to_mcp_error_response(error).as_dict()
        return {"stopped": True} if result is None else result.model_dump(mode="json")

    @staticmethod
    async def _status(service: ClangdService, _: ForgeModel) -> ForgeModel:
        return await service.status()

    @staticmethod
    async def _start(service: ClangdService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _StartArguments)
        return await service.start(request.compile_commands_dir)

    @staticmethod
    async def _stop(service: ClangdService, _: ForgeModel) -> None:
        await service.aclose()
        return None

    @staticmethod
    async def _diagnostics(service: ClangdService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _DiagnosticsArguments)
        return await service.diagnostics(request.path, timeout_seconds=request.timeout_seconds)

    @staticmethod
    async def _hover(service: ClangdService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _PositionArguments)
        return await service.hover(request.path, request.position)

    @staticmethod
    async def _definition(service: ClangdService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _PositionArguments)
        return await service.definition(request.path, request.position)

    @staticmethod
    async def _references(service: ClangdService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _ReferencesArguments)
        return await service.references(
            request.path, request.position, include_declaration=request.include_declaration
        )

    @staticmethod
    async def _document_symbols(service: ClangdService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _DocumentArguments)
        return await service.document_symbols(request.path)

    @staticmethod
    async def _workspace_symbols(service: ClangdService, request: ForgeModel) -> ForgeModel:
        assert isinstance(request, _WorkspaceSymbolsArguments)
        return await service.workspace_symbols(request.query, limit=request.limit)
