"""Structured debugger tools over explicitly selected session drivers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import uuid4

from mcp.server import MCPServer
from mcp.server.apps import Apps
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import BaseModel, Field

from forgemcp.assets import IconFile, Widget
from forgemcp.completion import Complete
from forgemcp.diagnostics import available_values, display_value
from forgemcp.progress import progress
from forgemcp.text import tool_result
from forgemcp.toolchain.errors import ToolchainError
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.spec import ToolSpec
from forgemcp.workspace.path import WorkspacePath

from . import models as m
from .errors import DebugError, DebugStateError, DebugTimeoutError
from .session import DebugSession
from .text import render

type SessionFactory = Callable[[str, ToolSpec], DebugSession]
type Timeout = Annotated[
    float,
    Field(
        gt=0,
        le=300,
        allow_inf_nan=False,
        description="timeout in seconds: finite, greater than 0 and at most 300.",
    ),
]


class DebugAdapter(BaseModel):
    """Identify one implemented adapter in one discovered toolset."""

    adapter: str
    toolset_id: str
    toolset_name: str


class SessionsResult(BaseModel):
    """A bounded window of retained session observations."""

    sessions: list[m.SessionSnapshot]
    next_start: int | None = None


class DebuggerService:
    """Debug native programs through explicitly selected toolsets and session IDs.

    Use debugger_adapters, then launch, attach, or open a user-mode dump.
    Execution commands return a reference for debugger_wait; a waiting timeout
    leaves the program running. Read inspection_generation from the current
    snapshot before continuing or requesting a stack. References expire when
    execution resumes. Close launched programs to terminate them; closing an
    attached session detaches. Inspect status.kind for stopped/exited/failed.
    Debugger tools are available to MCP clients only, not admin command controls.
    """

    WIDGET = Widget("assets/debugger-result.html")
    ICON = IconFile("icons/debugger.svg")
    MAX_SESSIONS = 128

    def __init__(
        self,
        toolchains: ToolchainService,
        factories: Mapping[str, SessionFactory],
        *,
        progress_interval: float = 1.0,
    ) -> None:
        """Retain explicitly wired drivers without starting debugger processes."""
        self.toolchains = toolchains
        self.factories = dict(factories)
        self.progress_interval = progress_interval
        self.sessions: dict[str, DebugSession] = {}
        self.closed = False

    def adapters(self) -> list[DebugAdapter]:
        """List supported adapters without launching probes or choosing a default toolset."""
        return [
            DebugAdapter(
                adapter=tool.name,
                toolset_id=toolset.id,
                toolset_name=toolset.name,
            )
            for toolset in self.toolchains.list_toolsets()
            for tool in toolset.tools
            if tool.name in self.factories
        ]

    def create_session(self, toolset_id: str, adapter: str) -> DebugSession:
        """Register a new session using only a selected supported ToolSpec."""
        if self.closed:
            raise DebugStateError(
                "The debugger service has closed. The operator must restart the server "
                "before opening another session."
            )
        if len(self.sessions) >= self.MAX_SESSIONS:
            raise DebugStateError(
                f"Retained debugger session limit ({self.MAX_SESSIONS}) reached. "
                "Call debugger_sessions() to inspect history; the operator must restart "
                "the server to release it. Closing a session retains its history.",
            )
        factory = self.factories.get(adapter)
        if factory is None:
            raise DebugStateError(
                f"Unsupported debugger adapter: adapter={display_value(adapter)}. "
                + available_values(self.factories, "debugger_adapters()")
            )
        tool = self.toolchains.get_tool(toolset_id, adapter)
        if tool is None:
            raise DebugStateError(
                f"toolset_id={display_value(toolset_id)} has no {adapter} adapter. "
                "Call debugger_adapters() to select an available toolset_id/adapter pair. "
                "If none are available, the operator must install LLDB-DAP and restart "
                "the server or configure --toolset."
            )
        identifier = uuid4().hex
        session = factory(identifier, tool)
        self.sessions[identifier] = session
        return session

    def get_session(self, session_id: str) -> DebugSession:
        """Resolve an explicit retained identity without changing any active-session selection."""
        try:
            return self.sessions[session_id]
        except KeyError as error:
            raise DebugStateError(
                f"Unknown debugger session: session_id={display_value(session_id)}. "
                + available_values(self.sessions, "debugger_sessions()")
                + " Session IDs belong to this server process; open a target first "
                "if no sessions are retained."
            ) from error

    def error_text(
        self,
        error: DebugError | ToolchainError,
        session: DebugSession | None = None,
    ) -> str:
        """Attach retained provenance and recovery without copying adapter payloads."""
        detail = str(error)
        if isinstance(error, DebugTimeoutError) and error.outcome_unknown:
            detail += " Command outcome is unknown; do not blindly repeat the mutation."
        process_id = getattr(error, "process_id", None)
        if session is not None:
            state = session.snapshot()
            detail += (
                f" Inspect debugger_get(session_id={display_value(state.session_id)}) "
                "before issuing another command."
            )
            process_id = state.adapter_process_id or process_id
        if process_id is not None:
            detail += f" Inspect process_get(process_id={process_id}) for the adapter transcript."
        return detail

    def snapshots(self, page: m.PageRequest) -> SessionsResult:
        """Read cached observations without contacting any adapter."""
        values = list(self.sessions.values())
        end = page.start + page.count
        return SessionsResult(
            sessions=[session.snapshot() for session in values[page.start:end]],
            next_start=end if end < len(values) else None,
        )

    async def close(self) -> None:
        """Close all sessions before the shared process service shuts down."""
        self.closed = True
        async with asyncio.TaskGroup() as group:
            for session in tuple(self.sessions.values()):
                group.create_task(session.close())

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        """Expose typed operations with one result-only App and readable text."""
        # Explicit SDK handlers keep public schemas and operation arguments visible here.
        # pylint: disable=too-many-locals,too-many-statements
        # pylint: disable=too-many-arguments,too-many-positional-arguments
        # Repeated SDK decoration is required by the explicit registration contract.
        # pylint: disable=duplicate-code
        del mcp, complete
        read_only = ToolAnnotations(read_only_hint=True, destructive_hint=False)
        changes_state = ToolAnnotations(read_only_hint=False, destructive_hint=True)

        @asynccontextmanager
        async def operation(
            ctx: Context,
            session_id: str | None = None,
        ) -> AsyncGenerator[None]:
            """Report one bounded operation and translate recoverable domain errors."""
            report = progress(ctx, interval=self.progress_interval)
            await report(0, total=1, message="Starting debugger operation")
            try:
                yield
            except (DebugError, ToolchainError) as error:
                raise ToolError(
                    self.error_text(error, self.sessions.get(session_id))
                ) from error
            await report(1, total=1, message="Debugger result ready")

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_adapters(
            ctx: Context,
        ) -> Annotated[CallToolResult, list[DebugAdapter]]:
            """List implemented adapters by toolset without probing executables."""
            async with operation(ctx):
                result = self.adapters()
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_sessions(
            ctx: Context,
            page: m.PageRequest | None = None,
        ) -> Annotated[CallToolResult, SessionsResult]:
            """List a bounded window of retained debugger session snapshots."""
            async with operation(ctx):
                result = self.snapshots(page or m.PageRequest())
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_get(
            session_id: str,
            ctx: Context,
        ) -> Annotated[CallToolResult, m.SessionSnapshot]:
            """Read one cached session state without contacting the debugger."""
            async with operation(ctx, session_id):
                result = self.get_session(session_id).snapshot()
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=changes_state,
        )
        async def debugger_launch(
            toolset_id: str,
            options: m.LaunchOptions,
            ctx: Context,
            adapter: str = "lldb-dap",
            initial: m.BreakpointPlan | None = None,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.ExecutionReceipt]:
            """Launch a project/storage native executable with initial breakpoints."""
            async with operation(ctx):
                session = self.create_session(toolset_id, adapter)
                try:
                    result = await session.launch(
                        options,
                        initial or m.BreakpointPlan(),
                        timeout=timeout,
                    )
                except BaseException as error:
                    await session.close()
                    if isinstance(error, (DebugError, ToolchainError)):
                        raise ToolError(self.error_text(error, session)) from error
                    raise
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=changes_state,
        )
        async def debugger_attach(
            toolset_id: str,
            options: m.AttachOptions,
            ctx: Context,
            adapter: str = "lldb-dap",
            initial: m.BreakpointPlan | None = None,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.ExecutionReceipt]:
            """Attach to an explicit local PID and configure initial breakpoints."""
            async with operation(ctx):
                session = self.create_session(toolset_id, adapter)
                try:
                    result = await session.attach(
                        options,
                        initial or m.BreakpointPlan(),
                        timeout=timeout,
                    )
                except BaseException as error:
                    await session.close()
                    if isinstance(error, (DebugError, ToolchainError)):
                        raise ToolError(self.error_text(error, session)) from error
                    raise
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=changes_state,
        )
        async def debugger_open_dump(
            toolset_id: str,
            options: m.DumpOptions,
            ctx: Context,
            adapter: str = "lldb-dap",
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.SessionSnapshot]:
            """Open a project/storage user-mode crash dump for inspection."""
            async with operation(ctx):
                session = self.create_session(toolset_id, adapter)
                try:
                    result = await session.open_dump(
                        options,
                        timeout=timeout,
                    )
                except BaseException as error:
                    await session.close()
                    if isinstance(error, (DebugError, ToolchainError)):
                        raise ToolError(self.error_text(error, session)) from error
                    raise
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=changes_state,
        )
        async def debugger_breakpoints(
            session_id: str,
            source: WorkspacePath,
            points: Annotated[list[m.SourceBreakpoint], Field(max_length=1000)],
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.BreakpointsResult]:
            """Replace every source-line breakpoint for one managed source file."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).set_breakpoints(
                    source,
                    points,
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=changes_state,
        )
        async def debugger_function_breakpoints(
            session_id: str,
            functions: Annotated[list[m.FunctionName], Field(max_length=1000)],
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.BreakpointsResult]:
            """Replace the function breakpoint names when the adapter supports them."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).set_function_breakpoints(
                    functions,
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=changes_state,
        )
        async def debugger_exception_breakpoints(
            session_id: str,
            filters: Annotated[list[str], Field(max_length=100)],
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.ExceptionBreakpointsResult]:
            """Select exception stop filters advertised by the current session."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).set_exception_breakpoints(
                    filters,
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=changes_state,
        )
        async def debugger_continue(
            session_id: str,
            stop: m.StopToken,
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.ExecutionReceipt]:
            """Continue from an expected stop; wait using the returned execution reference."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).resume(
                    stop,
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=changes_state,
        )
        async def debugger_step(
            session_id: str,
            thread: m.ThreadRef,
            stop: m.StopToken,
            kind: m.StepKind,
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.ExecutionReceipt]:
            """Step over, into, or out in one stopped thread."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).step(
                    thread,
                    stop,
                    kind,
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=changes_state,
        )
        async def debugger_pause(
            session_id: str,
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.ExecutionReceipt]:
            """Request a running program to stop without closing its session."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).pause(
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_threads(
            session_id: str,
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.ThreadsResult]:
            """List the current bounded thread identities."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).threads(
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_stack(
            session_id: str,
            thread: m.ThreadRef,
            stop: m.StopToken,
            ctx: Context,
            page: m.PageRequest | None = None,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.StackResult]:
            """Read a stack page from the expected suspended generation."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).stack(
                    thread,
                    stop,
                    page or m.PageRequest(),
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_scopes(
            session_id: str,
            frame: m.FrameRef,
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.ScopesResult]:
            """List the variable scopes belonging to a retained stack frame."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).scopes(
                    frame,
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_variables(
            session_id: str,
            reference: m.VariablesRef,
            ctx: Context,
            page: m.PageRequest | None = None,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.VariablesResult]:
            """Read a page of locals, arguments, or object children."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).variables(
                    reference,
                    page or m.PageRequest(),
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_modules(
            session_id: str,
            ctx: Context,
            page: m.PageRequest | None = None,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.ModulesResult]:
            """Read a page of loaded modules and available symbol information."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).modules(
                    page or m.PageRequest(),
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_exception_info(
            session_id: str,
            thread: m.ThreadRef,
            stop: m.StopToken,
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.ExceptionInfo]:
            """Inspect exception details for the current stopped thread."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).exception_info(
                    thread,
                    stop,
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_read_memory(
            session_id: str,
            reference: m.MemoryRef,
            offset: int,
            count: Annotated[int, Field(ge=1, le=65536)],
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.MemoryResult]:
            """Read a bounded memory range, preserving missing dump memory explicitly."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).read_memory(
                    reference,
                    offset,
                    count,
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_disassemble(
            session_id: str,
            reference: m.MemoryRef,
            offset: int,
            count: Annotated[int, Field(ge=1, le=1000)],
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.DisassemblyResult]:
            """Read a bounded sequence of instructions at an opaque memory reference."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).disassemble(
                    reference,
                    offset,
                    count,
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=changes_state,
        )
        async def debugger_close(
            session_id: str,
            ctx: Context,
            timeout: Timeout | None = None,
        ) -> Annotated[CallToolResult, m.CloseResult]:
            """Close by terminating a launched target, detaching, or releasing a dump."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).close(
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        @apps.tool(
            resource_uri=self.WIDGET.uri,
            icons=[self.ICON.icon],
            annotations=read_only,
        )
        async def debugger_wait(
            session_id: str,
            execution: m.ExecutionRef,
            ctx: Context,
            timeout: Timeout = 10.0,
        ) -> Annotated[CallToolResult, m.WaitResult]:
            """Wait for a stop or terminal observation; timeout leaves the target running."""
            async with operation(ctx, session_id):
                result = await self.get_session(session_id).wait_for_stop(
                    execution,
                    timeout=timeout,
                )
            return tool_result(result, render(result))

        apps.add_html_resource(self.WIDGET.uri, self.WIDGET.content)
