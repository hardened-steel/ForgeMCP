"""Builtin debugger plugin exposing only normalized ToolContributions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from pydantic import Field, ValidationError

from forgemcp.core.errors import ForgeMCPError, to_mcp_error_response
from forgemcp.debugger.backends import LldbDapBackend
from forgemcp.debugger.errors import DebuggerRequestError
from forgemcp.debugger.models import DebugBreakpointSpec, DebugLaunchRequest
from forgemcp.debugger.service import DebuggerService
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import ForgePlugin, PluginContext, PluginMetadata, ToolContribution
from forgemcp.processes import ProcessRuntime
from forgemcp.workspace import WorkspaceService


class _EmptyArguments(ForgeModel):
    """No-argument debugger tool schema."""


class _EventsArguments(ForgeModel):
    after_sequence: int | None = Field(default=None, ge=0, description="Optional previously returned event cursor.")
    limit: int = Field(default=100, ge=1, le=256, description="Maximum normalized events to return.")


class _LaunchBreakpoint(ForgeModel):
    line: int = Field(ge=0, description="Zero-based source line.")
    column: int | None = Field(default=None, ge=0, description="Optional zero-based source column.")


class _LaunchArguments(ForgeModel):
    program: str = Field(min_length=1, max_length=4096, description="Workspace-relative executable path.")
    cwd: str | None = Field(default=None, max_length=4096, description="Optional workspace-relative working directory.")
    args: list[str] = Field(default_factory=list, max_length=64, description="Bounded individual debuggee arguments, never a shell command.")
    environment: dict[str, str] = Field(default_factory=dict, max_length=32, description="Must be empty until a debuggee environment allow-list is configured.")
    stop_on_entry: bool = Field(default=True, description="Stop at program entry unless a breakpoint stops first.")
    initial_breakpoints: dict[str, list[_LaunchBreakpoint]] = Field(default_factory=dict, max_length=20, description="Full initial source breakpoint sets keyed by workspace-relative source path.")


class _BreakpointsArguments(ForgeModel):
    path: str = Field(min_length=1, max_length=4096, description="Workspace-relative source path.")
    breakpoints: list[_LaunchBreakpoint] = Field(max_length=100, description="Replacement source line/column breakpoint set in zero-based coordinates.")


class _ThreadArguments(ForgeModel):
    thread_id: str | None = Field(default=None, min_length=16, max_length=128, description="Optional opaque current-stop thread handle.")


class _RequiredThreadArguments(ForgeModel):
    thread_id: str = Field(min_length=16, max_length=128, description="Opaque current-stop thread handle.")


class _StackArguments(_RequiredThreadArguments):
    start_frame: int = Field(default=0, ge=0, le=10_000, description="Zero-based stack frame offset.")
    levels: int = Field(default=100, ge=1, le=100, description="Maximum returned stack frames.")


class _FrameArguments(ForgeModel):
    frame_id: str = Field(min_length=16, max_length=128, description="Opaque current-stop frame handle.")


class _VariablesArguments(ForgeModel):
    variables_id: str = Field(min_length=16, max_length=128, description="Opaque current-stop variables handle.")
    start: int = Field(default=0, ge=0, le=100_000, description="Variable paging offset.")
    count: int = Field(default=100, ge=1, le=200, description="Maximum variables to return.")


class _EvaluateArguments(_FrameArguments):
    expression: str = Field(min_length=1, max_length=1024, description="Conservative hover-only variable/member/index inspection expression.")


ToolOperation = Callable[[DebuggerService, ForgeModel], Awaitable[object]]


class DebuggerPlugin(ForgePlugin):
    """Application-scoped debugger plugin with no FastMCP or raw DAP access."""

    __slots__ = ("_service",)

    def __init__(self) -> None:
        super().__init__(PluginMetadata(plugin_id="debugger", requires_services=("workspace", "process_runtime"), provides=frozenset({"debugger"})))
        self._service: DebuggerService | None = None

    @property
    def service(self) -> DebuggerService:
        if self._service is None:
            raise RuntimeError("The debugger plugin is not running.")
        return self._service

    async def start(self, context: PluginContext) -> None:
        workspace = context.services.get("workspace")
        runtime = context.services.get("process_runtime")
        if not isinstance(workspace, WorkspaceService) or not isinstance(runtime, ProcessRuntime):
            raise TypeError("The debugger plugin requires WorkspaceService and ProcessRuntime.")
        self._service = DebuggerService(workspace, runtime, LldbDapBackend(context.config, context.logger, runtime))
        self._register_tools(context)

    async def stop(self) -> None:
        if self._service is not None:
            await self._service.aclose()
            self._service = None

    def _register_tools(self, context: PluginContext) -> None:
        contributions = (
            ("status", "Report the safe debugger state machine status.", _EmptyArguments, self._status),
            ("list_adapters", "List locally discovered debugger backends without launching them.", _EmptyArguments, self._list_adapters),
            ("launch", "Launch one workspace-contained debuggee through the approved LLDB-DAP backend.", _LaunchArguments, self._launch),
            ("stop", "Terminate the active debuggee and managed adapter tree idempotently.", _EmptyArguments, self._stop),
            ("set_breakpoints", "Replace source line/column breakpoints for one workspace source path.", _BreakpointsArguments, self._set_breakpoints),
            ("continue", "Resume the paused debuggee.", _ThreadArguments, self._continue),
            ("pause", "Request pause of a running debuggee.", _ThreadArguments, self._pause),
            ("step_over", "Step over from an opaque paused thread handle.", _RequiredThreadArguments, self._step_over),
            ("step_in", "Step into from an opaque paused thread handle.", _RequiredThreadArguments, self._step_in),
            ("step_out", "Step out from an opaque paused thread handle.", _RequiredThreadArguments, self._step_out),
            ("threads", "List opaque current-stop thread handles.", _EmptyArguments, self._threads),
            ("stack_trace", "List opaque current-stop frames for a thread.", _StackArguments, self._stack_trace),
            ("scopes", "List scopes and opaque variable handles for a frame.", _FrameArguments, self._scopes),
            ("variables", "Expand one opaque current-stop variables handle.", _VariablesArguments, self._variables),
            ("evaluate", "Evaluate a conservative hover-only inspection expression in a frame.", _EvaluateArguments, self._evaluate),
            ("events", "Read a bounded cursor page of normalized debugger events.", _EventsArguments, self._events),
        )
        for name, description, model, operation in contributions:
            context.tools.register(ToolContribution(name=name, description=description, input_model=model, handler=lambda arguments, m=model, op=operation: self._dispatch(m, arguments, op)))

    async def _dispatch(self, model: type[ForgeModel], arguments: Mapping[str, object], operation: ToolOperation) -> dict[str, object]:
        try:
            request = model.model_validate(arguments)
        except ValidationError:
            return to_mcp_error_response(DebuggerRequestError("Tool arguments do not match the published debugger schema.")).as_dict()
        try:
            result = await operation(self.service, request)
        except ForgeMCPError as error:
            return to_mcp_error_response(error).as_dict()
        if isinstance(result, ForgeModel):
            return result.model_dump(mode="json")
        if isinstance(result, Mapping):
            return dict(result)
        if isinstance(result, tuple):
            return {"items": [item.model_dump(mode="json") if isinstance(item, ForgeModel) else item for item in result]}
        return {"stopped": True} if result is None else {"items": result}

    @staticmethod
    async def _status(service: DebuggerService, _: ForgeModel) -> object:
        return await service.status()

    @staticmethod
    async def _list_adapters(service: DebuggerService, _: ForgeModel) -> object:
        return {"adapters": [item.model_dump(mode="json") for item in await service.list_adapters()]}

    @staticmethod
    async def _launch(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _LaunchArguments)
        return await service.launch(DebugLaunchRequest(
            program=request.program,
            cwd=request.cwd,
            args=tuple(request.args),
            environment=request.environment,
            stop_on_entry=request.stop_on_entry,
            initial_breakpoints={path: tuple(DebugBreakpointSpec(line=item.line, column=item.column) for item in items) for path, items in request.initial_breakpoints.items()},
        ))

    @staticmethod
    async def _stop(service: DebuggerService, _: ForgeModel) -> object:
        return await service.stop()

    @staticmethod
    async def _set_breakpoints(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _BreakpointsArguments)
        return {"breakpoints": [item.model_dump(mode="json") for item in await service.set_breakpoints(request.path, tuple(DebugBreakpointSpec(line=item.line, column=item.column) for item in request.breakpoints))]}

    @staticmethod
    async def _continue(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _ThreadArguments)
        return await service.continue_execution(request.thread_id)

    @staticmethod
    async def _pause(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _ThreadArguments)
        return await service.pause(request.thread_id)

    @staticmethod
    async def _step_over(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _RequiredThreadArguments)
        return await service.step_over(request.thread_id)

    @staticmethod
    async def _step_in(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _RequiredThreadArguments)
        return await service.step_in(request.thread_id)

    @staticmethod
    async def _step_out(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _RequiredThreadArguments)
        return await service.step_out(request.thread_id)

    @staticmethod
    async def _threads(service: DebuggerService, _: ForgeModel) -> object:
        return {"threads": [item.model_dump(mode="json") for item in await service.threads()]}

    @staticmethod
    async def _stack_trace(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _StackArguments)
        return {"frames": [item.model_dump(mode="json") for item in await service.stack_trace(request.thread_id, start_frame=request.start_frame, levels=request.levels)]}

    @staticmethod
    async def _scopes(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _FrameArguments)
        return {"scopes": [item.model_dump(mode="json") for item in await service.scopes(request.frame_id)]}

    @staticmethod
    async def _variables(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _VariablesArguments)
        return {"variables": [item.model_dump(mode="json") for item in await service.variables(request.variables_id, start=request.start, count=request.count)]}

    @staticmethod
    async def _evaluate(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _EvaluateArguments)
        return await service.evaluate(request.frame_id, request.expression)

    @staticmethod
    async def _events(service: DebuggerService, request: ForgeModel) -> object:
        assert isinstance(request, _EventsArguments)
        return await service.events(after_sequence=request.after_sequence, limit=request.limit)
