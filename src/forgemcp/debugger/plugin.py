"""Builtin debugger plugin exposing only normalized ToolContributions."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping

from pydantic import ConfigDict, Field, ValidationError

from forgemcp.core.errors import ForgeMCPError, to_mcp_error_response
from forgemcp.debugger.backends import LldbDapBackend
from forgemcp.debugger.errors import DebuggerRequestError
from forgemcp.debugger.models import DebugBreakpointSpec, DebugLaunchRequest
from forgemcp.debugger.service import DebuggerService
from forgemcp.models._base import ForgeModel
from forgemcp.plugins import (
    ForgePlugin,
    NoOpProgressReporter,
    PluginContext,
    PluginMetadata,
    ProgressUpdate,
    ToolContribution,
    ToolExecutionContext,
)
from forgemcp.project import ComponentState, ComponentStatus, ProjectStatusRegistry, StatusFact
from forgemcp.project.models import utc_now
from forgemcp.processes import ProcessRuntime
from forgemcp.toolchain import ToolchainDiscoveryService
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
    # The evaluate allow-list is intentionally lexical: whitespace must reach
    # DebuggerService unchanged so it is rejected rather than normalized into
    # a different expression.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=False)

    expression: str = Field(min_length=1, max_length=1024, description="Single ASCII identifier lookup in the selected frame; native evaluation can still have side effects.")


ToolOperation = Callable[..., Awaitable[object]]


async def _run_lifecycle_progress(
    context: ToolExecutionContext, label: str, operation: Awaitable[object]
) -> object:
    """Report bounded phase/elapsed state while a debugger lifecycle changes."""
    await context.report_progress(ProgressUpdate(0, None, label))
    heartbeat: asyncio.Task[None] | None = None
    if context.supports_progress:
        async def pulse() -> None:
            started = asyncio.get_running_loop().time()
            while True:
                await asyncio.sleep(2.0)
                await context.report_progress(
                    ProgressUpdate(0, None, f"{label} ({max(1, int(asyncio.get_running_loop().time() - started))}s)")
                )
        heartbeat = asyncio.create_task(pulse())
    try:
        result = await operation
    except asyncio.CancelledError:
        await context.report_progress(ProgressUpdate(0, None, f"{label} cancelled", terminal=True))
        raise
    except Exception:
        await context.report_progress(ProgressUpdate(0, None, f"{label} failed", terminal=True))
        raise
    else:
        await context.report_progress(
            ProgressUpdate(1, None, f"{label} completed", terminal=True, completed=True)
        )
        return result
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)


class _DebuggerStatusProvider:
    id = "debugger"

    def __init__(self, service: DebuggerService, *, explicitly_configured: bool) -> None:
        self._service = service
        self._explicitly_configured = explicitly_configured

    async def snapshot_status(self) -> ComponentStatus:
        cached = await self._service.cached_project_status()
        native_state = cached.session.state.value
        state = (
            ComponentState.UNAVAILABLE
            if native_state == "unavailable"
            else ComponentState.STOPPED
            if native_state in {"stopped", "terminated"}
            else ComponentState.STARTING
            if native_state in {"starting", "initialized", "configuring"}
            else ComponentState.ACTIVE
            if native_state in {"running", "terminating"}
            else ComponentState.PAUSED
            if native_state == "paused"
            else ComponentState.FAILED
        )
        if state is ComponentState.UNAVAILABLE and self._explicitly_configured:
            state = ComponentState.DEGRADED
        facts = [
            StatusFact(name="adapter_available", value=cached.adapter.available),
            StatusFact(name="session_state", value=native_state),
            StatusFact(name="unread_events", value=cached.unread_event_count),
            StatusFact(name="dropped_events", value=cached.dropped_event_count),
        ]
        if cached.adapter.version is not None:
            facts.append(StatusFact(name="adapter_version", value=cached.adapter.version))
        if cached.stop_reason is not None:
            facts.append(StatusFact(name="stop_reason", value=cached.stop_reason.value))
        if cached.thread_count is not None:
            facts.append(StatusFact(name="thread_count", value=cached.thread_count))
        return ComponentStatus(
            id=self.id,
            display_name="Native Debugger",
            state=state,
            capabilities=("debugger.launch", "debugger.execution", "debugger.paused_inspection"),
            summary="Cached debugger adapter, session, and bounded event counters.",
            facts=tuple(facts),
            warnings=(
                ("debugger_session_failed",)
                if state is ComponentState.FAILED
                else ("required_capability_unavailable",)
                if state is ComponentState.DEGRADED
                else ()
            ),
            observed_at=utc_now(),
        )


class DebuggerPlugin(ForgePlugin):
    """Application-scoped debugger plugin with no FastMCP or raw DAP access."""

    __slots__ = ("_service", "_status_registry")

    def __init__(self) -> None:
        super().__init__(PluginMetadata(plugin_id="debugger", requires_services=("workspace", "process_runtime", "toolchain_discovery", "project_status_registry"), provides=frozenset({"debugger"})))
        self._service: DebuggerService | None = None
        self._status_registry: ProjectStatusRegistry | None = None

    @property
    def service(self) -> DebuggerService:
        if self._service is None:
            raise RuntimeError("The debugger plugin is not running.")
        return self._service

    async def start(self, context: PluginContext) -> None:
        workspace = context.services.get("workspace")
        runtime = context.services.get("process_runtime")
        toolchain = context.services.get("toolchain_discovery")
        status_registry = context.services.get("project_status_registry")
        if not isinstance(workspace, WorkspaceService) or not isinstance(runtime, ProcessRuntime):
            raise TypeError("The debugger plugin requires WorkspaceService and ProcessRuntime.")
        if not isinstance(status_registry, ProjectStatusRegistry):
            raise TypeError("The debugger plugin requires ProjectStatusRegistry.")
        if not isinstance(toolchain, ToolchainDiscoveryService):
            raise TypeError("The debugger plugin requires ToolchainDiscoveryService.")
        self._service = DebuggerService(workspace, runtime, LldbDapBackend(context.config, context.logger, runtime, toolchain))
        self._status_registry = status_registry
        status_registry.register(
            _DebuggerStatusProvider(
                self._service, explicitly_configured=context.config.lldb_dap_path is not None
            )
        )
        self._register_tools(context)

    async def stop(self) -> None:
        if self._status_registry is not None:
            self._status_registry.unregister("debugger")
            self._status_registry = None
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
            ("evaluate", "Evaluate one identifier lookup in a frame; native evaluation may still have side effects.", _EvaluateArguments, self._evaluate),
            ("events", "Read a bounded cursor page of normalized debugger events.", _EventsArguments, self._events),
        )
        for name, description, model, operation in contributions:
            context.tools.register(ToolContribution(name=name, description=description, input_model=model, handler=lambda arguments, m=model, op=operation, *, execution_context=None: self._dispatch(m, arguments, op, execution_context)))

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
            return to_mcp_error_response(DebuggerRequestError("Tool arguments do not match the published debugger schema.")).as_dict()
        try:
            context = execution_context or ToolExecutionContext(NoOpProgressReporter())
            if operation.__name__ in {"_launch", "_stop"}:
                result = await operation(self.service, request, context)
            else:
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
    async def _launch(
        service: DebuggerService, request: ForgeModel, context: ToolExecutionContext
    ) -> object:
        assert isinstance(request, _LaunchArguments)
        return await _run_lifecycle_progress(context, "Launching debugger", service.launch(DebugLaunchRequest(
            program=request.program,
            cwd=request.cwd,
            args=tuple(request.args),
            environment=request.environment,
            stop_on_entry=request.stop_on_entry,
            initial_breakpoints={path: tuple(DebugBreakpointSpec(line=item.line, column=item.column) for item in items) for path, items in request.initial_breakpoints.items()},
        )))

    @staticmethod
    async def _stop(service: DebuggerService, _: ForgeModel, context: ToolExecutionContext) -> object:
        return await _run_lifecycle_progress(context, "Stopping debugger", service.stop())

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
