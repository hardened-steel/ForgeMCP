"""Connect one running MCP instance to the shared local administration hub."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
import inspect
import json
import sys
from typing import Any, Literal
from uuid import uuid4

from mcp import Client
from mcp.client import ClientRequestContext, advertise
from mcp.server import MCPServer
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID
from mcp.shared.exceptions import MCPError
from mcp.types import CompleteRequestParams, ElicitRequestParams, ElicitResult
from pydantic import BaseModel, Field

from forgemcp.local_ipc import IpcConnection, IpcError, connect
from forgemcp.process.errors import ProcessError

from .history import MemoryHistory, ToolCall, bridge_request_id, call_source


class RpcResponse(BaseModel):
    """Return either an SDK result or a protocol error to the local hub."""

    type: Literal["response"] = "response"
    id: str | int | None
    result: Any = None
    error: Any = None


class Registration(BaseModel):
    """Identify the independently running process to its user-scoped hub."""

    type: Literal["register"] = "register"
    instance: dict[str, Any]


class ChangeEvent(BaseModel):
    """Tell the hub to fetch newer state without duplicating result payloads."""

    type: Literal["event"] = "event"
    kind: Literal["history_changed", "elicitation_changed", "state_changed"]
    version: int


class ElicitationPrompt(BaseModel):
    """One pending SDK question awaiting an answer from the browser."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    request_id: str | int | None
    params: Any
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InstanceAgent:
    """Reuse one SDK client for hub commands without owning the service runtime."""

    ELICITATION_TIMEOUT = 300.0
    RETRY_INITIAL = 0.1
    RETRY_MAXIMUM = 5.0
    HANDSHAKE_TIMEOUT = 10.0
    STATE_INTERVAL = 2.0

    def __init__(
        self,
        mcp: MCPServer,
        history: MemoryHistory,
        metadata: Mapping[str, Any],
        *,
        state_snapshot: Callable[
            [],
            dict[str, Any] | Awaitable[dict[str, Any]],
        ] | None,
        ensure_hub: Callable[[], Awaitable[None]],
        ipc_name: str,
    ) -> None:
        """Bind the existing server, journal, process identity, and hub launcher."""
        self.mcp = mcp
        self.history = history
        self.metadata = dict(metadata)
        self.state_snapshot = state_snapshot
        self.ensure_hub = ensure_hub
        self.ipc_name = ipc_name
        self.stopping = asyncio.Event()
        self.connection: IpcConnection | None = None
        self.run_task: asyncio.Task[None] | None = None
        self.requests: set[asyncio.Task[None]] = set()
        self.active_requests: dict[str | int, asyncio.Task[None]] = {}
        self.elicitations: dict[
            str,
            tuple[ElicitationPrompt, asyncio.Future[ElicitResult]],
        ] = {}
        self.elicitation_version = 0
        self.elicitation_changed = asyncio.Event()
        self.state_version = 0
        self.hub_id: str | None = None
        self.hub_url: str | None = None

    async def run(self) -> None:
        """Keep one SDK connection while reconnecting the independent IPC bridge."""
        if self.run_task is not None:
            raise RuntimeError("The instance agent is already running.")
        self.run_task = asyncio.current_task()
        retry = self.RETRY_INITIAL
        try:
            async with Client(
                self.mcp,
                cache=None,
                read_timeout_seconds=None,
                elicitation_callback=self.elicit,
                extensions=[advertise(EXTENSION_ID, {"mimeTypes": [APP_MIME_TYPE]})],
            ) as client:
                try:
                    while not self.stopping.is_set():
                        connection = None
                        try:
                            connection = await connect(self.ipc_name)
                            self.connection = connection
                            await self.serve_connection(connection, client)
                            retry = self.RETRY_INITIAL
                        except (IpcError, OSError, ValueError):
                            pass
                        finally:
                            self.connection = None
                            if connection is not None:
                                await connection.close()
                        if self.stopping.is_set():
                            break
                        try:
                            await self.ensure_hub()
                        except (IpcError, OSError, RuntimeError, ProcessError):
                            pass
                        try:
                            await asyncio.wait_for(self.stopping.wait(), timeout=retry)
                        except TimeoutError:
                            retry = min(retry * 2, self.RETRY_MAXIMUM)
                finally:
                    for task in tuple(self.requests):
                        task.cancel()
                    if self.requests:
                        await asyncio.gather(*tuple(self.requests), return_exceptions=True)
                    for _, future in tuple(self.elicitations.values()):
                        future.cancel()
        finally:
            self.run_task = None

    async def close(self) -> None:
        """Stop reconnection and wait for SDK requests to finish their cancellation cleanup."""
        self.stopping.set()
        if self.connection is not None:
            try:
                async with asyncio.timeout(1.0):
                    await self.connection.send(b'{"type":"unregister"}')
            except (IpcError, OSError, TimeoutError):
                pass
            await self.connection.close()
        task = self.run_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def serve_connection(
        self,
        connection: IpcConnection,
        client: Client,
    ) -> None:
        """Register the instance and dispatch requests concurrently until IPC closes."""
        await self.send(
            connection,
            Registration(type="register", instance=self.metadata),
        )
        async with asyncio.timeout(self.HANDSHAKE_TIMEOUT):
            acknowledgement = await self.receive(connection)
        if acknowledgement is None or acknowledgement.get("type") != "registered":
            raise ValueError("The hub did not acknowledge instance registration.")
        hub_id = acknowledgement.get("hub_id")
        url = acknowledgement.get("url")
        if not isinstance(hub_id, str) or not isinstance(url, str):
            raise ValueError("The hub registration acknowledgement is invalid.")
        self.hub_id = hub_id
        if url != self.hub_url:
            self.hub_url = url
            print(
                f"ForgeMCP admin: {url.split('#', 1)[0]} (forgemcp --open-admin)",
                file=sys.stderr,
                flush=True,
            )
        history_updates = asyncio.create_task(self.watch_history(connection))
        input_updates = asyncio.create_task(self.watch_elicitations(connection))
        state_updates = asyncio.create_task(self.watch_state(connection))
        try:
            while not self.stopping.is_set():
                message = await self.receive(connection)
                if message is None:
                    break
                if message.get("type") != "request":
                    continue
                task = asyncio.create_task(self.handle_request(connection, client, message))
                self.requests.add(task)
                task.add_done_callback(self.requests.discard)
        finally:
            history_updates.cancel()
            input_updates.cancel()
            state_updates.cancel()
            await asyncio.gather(
                history_updates,
                input_updates,
                state_updates,
                return_exceptions=True,
            )

    async def send(self, connection: IpcConnection, message: BaseModel) -> None:
        """Omit absent SDK fields while preserving JSON null values and model defaults."""
        payload = message.model_dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(message, RpcResponse):
            selected = "error" if "error" in message.model_fields_set else "result"
            unused = "result" if selected == "error" else "error"
            payload.pop(unused, None)
            payload.setdefault(selected, None)
            payload["id"] = message.id
        await connection.send(
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )

    async def receive(self, connection: IpcConnection) -> dict[str, Any] | None:
        """Decode one hub envelope, preserving clean EOF as None."""
        payload = await connection.receive()
        if payload is None:
            return None
        message = json.loads(payload.decode("utf-8"))
        if not isinstance(message, dict):
            raise ValueError("Hub messages must be objects.")
        return message

    async def watch_history(self, connection: IpcConnection) -> None:
        """Send journal invalidations instead of copying tool results into events."""
        version = self.history.version
        try:
            while True:
                version = await self.history.wait_for_change(version)
                await self.send(
                    connection,
                    ChangeEvent(type="event", kind="history_changed", version=version),
                )
        except (IpcError, OSError):
            return

    async def watch_elicitations(self, connection: IpcConnection) -> None:
        """Notify the hub when pending questions appear or are withdrawn."""
        try:
            while True:
                await self.elicitation_changed.wait()
                self.elicitation_changed.clear()
                await self.send(
                    connection,
                    ChangeEvent(
                        type="event",
                        kind="elicitation_changed",
                        version=self.elicitation_version,
                    ),
                )
        except (IpcError, OSError):
            return

    async def watch_state(self, connection: IpcConnection) -> None:
        """Invalidate lightweight service monitors while subprocesses are still running."""
        try:
            while True:
                await asyncio.sleep(self.STATE_INTERVAL)
                self.state_version += 1
                await self.send(
                    connection,
                    ChangeEvent(
                        type="event",
                        kind="state_changed",
                        version=self.state_version,
                    ),
                )
        except (IpcError, OSError):
            return

    async def handle_request(
        self,
        connection: IpcConnection,
        client: Client,
        message: dict[str, Any],
    ) -> None:
        """Answer one request without replaying it when the originating IPC link is lost."""
        request_id = message.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, str | int):
            request_id = None
        source_token = call_source.set("admin")
        request_token = bridge_request_id.set(request_id)
        try:
            method = message.get("method")
            params = message.get("params", {})
            if request_id is None or not isinstance(method, str) or not isinstance(params, dict):
                raise MCPError(code=-32600, message="Invalid instance request.")
            self.active_requests[request_id] = asyncio.current_task()
            result = await self.dispatch(client, method, params)
            response = RpcResponse(type="response", id=request_id, result=result)
        except asyncio.CancelledError:
            response = RpcResponse(
                type="response",
                id=request_id,
                error={"code": -32800, "message": "Admin request was cancelled."},
            )
        except MCPError as error:
            response = RpcResponse(type="response", id=request_id, error=error.error)
        except (ValueError, TypeError, KeyError):
            response = RpcResponse(
                type="response",
                id=request_id,
                error={"code": -32602, "message": "Invalid instance request parameters."},
            )
        except Exception:
            response = RpcResponse(
                type="response",
                id=request_id,
                error={"code": -32603, "message": "Instance request failed."},
            )
        finally:
            self.active_requests.pop(request_id, None)
            bridge_request_id.reset(request_token)
            call_source.reset(source_token)
        try:
            await self.send(connection, response)
        except (IpcError, OSError):
            pass

    async def dispatch(
        self,
        client: Client,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        """Delegate the small administration surface to existing SDK client operations."""
        if method == "snapshot":
            tools, resources, templates = await asyncio.gather(
                client.list_tools(),
                client.list_resources(),
                client.list_resource_templates(),
            )
            return {
                "tools": [tool for tool in tools.tools if not tool.name.startswith("debugger_")],
                "resources": resources.resources,
                "resource_templates": templates.resource_templates,
                "history": self.history.list_calls(),
                "state": await self.read_state(),
            }
        if method == "tools/list":
            tools = await client.list_tools()
            return tools.model_copy(
                update={
                    "tools": [
                        tool
                        for tool in tools.tools
                        if not tool.name.startswith("debugger_")
                    ],
                },
            )
        if method == "resources/list":
            return await client.list_resources()
        if method == "resources/templates/list":
            return await client.list_resource_templates()
        if method == "completion/complete":
            request = CompleteRequestParams.model_validate(params)
            return await client.complete(
                ref=request.ref,
                argument=request.argument.model_dump(),
                context_arguments=request.context.arguments if request.context else None,
            )
        if method == "resources/read":
            uri = params["uri"]
            if not isinstance(uri, str):
                raise ValueError("Resource URI must be a string.")
            call_id = params.get("call_id")
            if call_id is not None:
                call = self.history.get_call(call_id) if isinstance(call_id, str) else None
                if call is None or not self.is_linked_resource(call, uri):
                    raise MCPError(code=-32602, message="Resource is not linked by this tool call.")
            return await client.read_resource(uri)
        if method == "tools/call":
            name = params["name"]
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise ValueError("Tool name and arguments are invalid.")
            if name.startswith("debugger_"):
                raise MCPError(
                    code=-32601,
                    message="Debugger tools are unavailable through the admin panel.",
                )
            return await client.call_tool(name, arguments)
        if method == "history/list":
            return {"calls": self.history.list_calls()}
        if method == "calls/get":
            call = self.history.get_call(params["id"])
            if call is None:
                raise MCPError(code=-32004, message="Tool call is not retained.")
            return call
        if method == "calls/cancel":
            call = self.history.get_call(params["id"])
            if call is None or call.source != "admin":
                raise MCPError(code=-32602, message="Only this admin client's calls can be cancelled.")
            task = self.active_requests.get(call.admin_request_id)
            if call.status != "running" or task is None:
                return {"cancelled": False}
            task.cancel()
            return {"cancelled": True}
        if method in ("state", "state/get"):
            return await self.read_state()
        if method == "elicitation/list":
            return {"requests": [prompt for prompt, _ in self.elicitations.values()]}
        if method == "elicitation/respond":
            entry = self.elicitations.get(params["id"])
            if entry is None or entry[1].done():
                raise MCPError(code=-32004, message="Elicitation is no longer pending.")
            entry[1].set_result(ElicitResult.model_validate(params["result"]))
            return {"accepted": True}
        raise MCPError(code=-32601, message="Unknown instance method.")

    async def read_state(self) -> dict[str, Any]:
        """Read the composition root's lightweight snapshot without running MCP tools."""
        if self.state_snapshot is None:
            return {}
        state = self.state_snapshot()
        return await state if inspect.isawaitable(state) else state

    @staticmethod
    def is_linked_resource(call: ToolCall, uri: str) -> bool:
        """Allow widget reads only for immutable resources linked by the original result."""
        if not uri.startswith("forgemcp://workspace/results/"):
            return False
        result = call.result
        structured = (
            result.get("structuredContent")
            if isinstance(result, dict)
            else getattr(result, "structured_content", None)
        )
        if not isinstance(structured, dict):
            return False
        resources = structured.get("resources")
        if not isinstance(resources, dict):
            return False
        return any(
            isinstance(descriptor, dict) and descriptor.get("uri") == uri
            for descriptor in resources.values()
        )

    async def elicit(
        self,
        context: ClientRequestContext,
        params: ElicitRequestParams,
    ) -> ElicitResult:
        """Wait for a browser response to one SDK form or URL elicitation."""
        del context
        prompt = ElicitationPrompt(request_id=bridge_request_id.get(), params=params)
        future: asyncio.Future[ElicitResult] = asyncio.get_running_loop().create_future()
        self.elicitations[prompt.id] = (prompt, future)
        self.elicitation_version += 1
        self.elicitation_changed.set()
        try:
            async with asyncio.timeout(self.ELICITATION_TIMEOUT):
                return await future
        except TimeoutError:
            return ElicitResult(action="cancel")
        finally:
            self.elicitations.pop(prompt.id, None)
            self.elicitation_version += 1
            self.elicitation_changed.set()
