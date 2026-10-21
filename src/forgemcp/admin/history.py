"""Retain SDK tool calls and their progress for the instance administration view."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field

from forgemcp.progress import progress_collector


call_source: ContextVar[Literal["stdio", "admin"]] = ContextVar(
    "forgemcp_call_source",
    default="stdio",
)
bridge_request_id: ContextVar[str | int | None] = ContextVar(
    "forgemcp_admin_request_id",
    default=None,
)


class CallProgress(BaseModel):
    """One progress update observed while a tool call was running."""

    value: float
    total: float | None = None
    message: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ToolCall(BaseModel):
    """A single SDK request, retaining its original result object in memory."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    tool_name: str
    arguments: dict[str, Any]
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    status: Literal[
        "running",
        "completed",
        "failed",
        "cancelled",
        "input_required",
    ] = "running"
    result: Any = None
    error: str | None = None
    progress: list[CallProgress] = Field(default_factory=list)
    source: Literal["stdio", "admin"] = "stdio"
    admin_request_id: str | int | None = None


class MemoryHistory:
    """Observe SDK requests without changing their execution or concurrency."""

    def __init__(self) -> None:
        """Start an empty call journal with one coalescing change notification."""
        self.calls: dict[str, ToolCall] = {}
        self.version = 0
        self.changed = asyncio.Event()

    def list_calls(self) -> list[ToolCall]:
        """Return retained calls in their original admission order."""
        return list(self.calls.values())

    def get_call(self, call_id: str) -> ToolCall | None:
        """Return one retained invocation without replaying it."""
        return self.calls.get(call_id)

    def notify_changed(self) -> None:
        """Advance the journal version and wake the administration bridge."""
        self.version += 1
        self.changed.set()

    async def wait_for_change(self, after_version: int) -> int:
        """Wait until a newer journal version is available without retaining events."""
        while self.version <= after_version:
            self.changed.clear()
            if self.version > after_version:
                break
            await self.changed.wait()
        return self.version

    async def __call__(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        """Use the SDK middleware contract for tool observation only."""
        if ctx.method != "tools/call":
            return await call_next(ctx)
        return await self.on_call_tool(ctx, call_next)

    async def on_call_tool(
        self,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        """Record a request and its terminal SDK result while preserving exceptions."""
        parameters = ctx.params or {}
        arguments = parameters.get("arguments")
        call = ToolCall(
            tool_name=str(parameters.get("name", "")),
            arguments=deepcopy(arguments) if isinstance(arguments, dict) else {},
            source=call_source.get(),
            admin_request_id=bridge_request_id.get(),
        )
        self.calls[call.id] = call
        self.notify_changed()

        def collect_progress(
            value: float,
            total: float | None,
            message: str | None,
        ) -> None:
            """Attach reported work to this invocation and coalesce bridge updates."""
            call.progress.append(CallProgress(value=value, total=total, message=message))
            self.notify_changed()

        token = progress_collector.set(collect_progress)
        try:
            result = await call_next(ctx)
            call.result = result
            if isinstance(result, BaseModel):
                failed = bool(getattr(result, "is_error", False))
                needs_input = getattr(result, "result_type", None) == "input_required"
            elif isinstance(result, dict):
                failed = bool(result.get("isError", False))
                needs_input = result.get("resultType") == "input_required"
            else:
                failed = False
                needs_input = False
            call.status = "input_required" if needs_input else "failed" if failed else "completed"
            return result
        except asyncio.CancelledError:
            call.status = "cancelled"
            call.error = "Tool call was cancelled."
            raise
        except MCPError as error:
            call.status = "failed"
            call.error = str(error)
            raise
        except Exception:
            call.status = "failed"
            call.error = "Tool call failed unexpectedly."
            raise
        finally:
            progress_collector.reset(token)
            call.finished_at = datetime.now(timezone.utc)
            self.notify_changed()
