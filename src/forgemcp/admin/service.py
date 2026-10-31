"""Local HTTP panel routing requests to independent ForgeMCP runtimes over IPC."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import socket
from collections.abc import AsyncGenerator
from contextlib import suppress
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any
from uuid import uuid4

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from forgemcp import local_ipc
from forgemcp.process.host import ProcessIdentity

from .errors import HubRequestError

logger = logging.getLogger(__name__)


@dataclass
class HubInstance:
    """A registered process identity and the current connection's outstanding requests."""

    metadata: dict[str, Any]
    identity: ProcessIdentity
    connection: local_ipc.IpcConnection | None = None
    pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        """Expose only registration metadata and current connection availability."""
        return {**self.metadata, "connected": self.connection is not None}

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        """Correlate one request; lost responses are never replayed on a new connection."""
        connection = self.connection
        if connection is None:
            raise HubRequestError("This ForgeMCP instance is reconnecting.")
        identifier = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await connection.send(
                json.dumps(
                    {
                        "type": "request",
                        "id": identifier,
                        "method": method,
                        "params": params,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            )
            return await future
        except local_ipc.IpcError as error:
            raise HubRequestError(
                "The instance connection was lost. Check call history before repeating a command."
            ) from error
        finally:
            self.pending.pop(identifier, None)

    def disconnected(self, connection: local_ipc.IpcConnection) -> None:
        """Fail outstanding responses only if this is still the registered connection."""
        if self.connection is not connection:
            return
        self.connection = None
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(
                    HubRequestError(
                        "The instance connection was lost; command completion is unknown. Check history after reconnecting."
                    )
                )


class HubService:
    """Aggregate independent local instances; browsers do not extend the Hub lifetime."""

    STARTUP_GRACE = 15.0
    EXIT_GRACE = 3.0

    def __init__(
        self,
        listener: local_ipc.IpcListener,
        *,
        port: int = 8765,
    ) -> None:
        """Bind an already-owned IPC listener and initialize volatile panel state."""
        self.listener = listener
        self.port = port
        self.instances: dict[str, HubInstance] = {}
        self.hub_id = str(uuid4())
        self.bootstrap_token = secrets.token_urlsafe(32)
        self.session_token = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.cookie_name = f"forgemcp_admin_{self.hub_id.replace('-', '')}"
        self.origin = ""
        self.sandbox_origin = ""
        self.url = ""
        self.lock = asyncio.Lock()
        self.changed = asyncio.Condition()
        self.revision = 0
        self.last_event: dict[str, Any] = {"kind": "ready"}
        self.stopping = asyncio.Event()
        self.closing = False
        self.ever_registered = False
        self.empty_since: float | None = None
        self.connections: set[local_ipc.IpcConnection] = set()
        self.tasks: set[asyncio.Task[None]] = set()

    async def publish(self, kind: str, instance_id: str | None = None) -> None:
        """Wake browser streams with a small invalidation rather than copying results."""
        async with self.changed:
            self.revision += 1
            self.last_event = {"kind": kind, "instance_id": instance_id}
            self.changed.notify_all()

    async def accept_connections(self) -> None:
        """Accept independent duplex connections while holding the singleton listener."""
        while not self.stopping.is_set():
            connection = await self.listener.accept()
            self.connections.add(connection)
            task = asyncio.create_task(self.serve_connection(connection))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def serve_connection(self, connection: local_ipc.IpcConnection) -> None:
        """Authenticate registration metadata against the OS peer and route responses."""
        instance: HubInstance | None = None
        instance_id: str | None = None
        try:
            async with asyncio.timeout(10):
                payload = await connection.receive()
            if payload is None:
                return
            message = json.loads(payload)
            if message.get("type") == "discover":
                await connection.send(
                    json.dumps({"type": "discovered", "url": self.url}).encode()
                )
                return
            if message.get("type") != "register":
                return
            metadata = message["instance"]
            instance_id = str(metadata["id"])
            identity = ProcessIdentity(
                int(metadata["pid"]),
                float(metadata["process_started_at"]),
            )
            if connection.peer_pid is not None and connection.peer_pid != identity.pid:
                return
            if not identity.is_alive():
                return
            previous: local_ipc.IpcConnection | None = None
            async with self.lock:
                if self.closing:
                    return
                instance = self.instances.get(instance_id)
                if instance is not None and instance.identity != identity:
                    return
                if instance is None:
                    instance = HubInstance(metadata, identity)
                    self.instances[instance_id] = instance
                previous = instance.connection
                if previous is not None:
                    instance.disconnected(previous)
                instance.connection = connection
                self.empty_since = None
                self.ever_registered = True
            if previous is not None:
                await previous.close()
            await connection.send(
                json.dumps(
                    {"type": "registered", "hub_id": self.hub_id, "url": self.url}
                ).encode()
            )
            await self.publish("registered", instance_id)
            while not self.stopping.is_set():
                payload = await connection.receive()
                if payload is None:
                    break
                message = json.loads(payload)
                if message.get("type") == "response":
                    future = instance.pending.get(str(message.get("id")))
                    if future is not None and not future.done():
                        if "error" in message:
                            error = message["error"]
                            text = error.get("message", "The instance rejected the request.")
                            future.set_exception(HubRequestError(str(text), 400))
                        else:
                            future.set_result(message.get("result"))
                elif message.get("type") == "event":
                    await self.publish(str(message.get("kind", "changed")), instance_id)
                elif message.get("type") == "unregister":
                    async with self.lock:
                        if instance.connection is connection:
                            self.instances.pop(instance_id, None)
                    break
        except (
            local_ipc.IpcError,
            TimeoutError,
            ValueError,
            TypeError,
            KeyError,
        ):
            logger.debug("An admin IPC connection ended before completing its exchange.")
        finally:
            if instance is not None:
                instance.disconnected(connection)
                await self.publish("disconnected", instance_id)
            self.connections.discard(connection)
            await connection.close()

    async def watch_lifetime(self) -> None:
        """Retain disconnected live processes and stop after the final runtime ends."""
        started = asyncio.get_running_loop().time()
        while not self.stopping.is_set():
            await asyncio.sleep(0.5)
            removed: list[str] = []
            async with self.lock:
                for identifier, instance in tuple(self.instances.items()):
                    if not instance.identity.is_alive():
                        removed.append(identifier)
                        self.instances.pop(identifier)
                        if instance.connection is not None:
                            await instance.connection.close()
                now = asyncio.get_running_loop().time()
                if self.instances:
                    self.empty_since = None
                elif not self.ever_registered:
                    if now - started >= self.STARTUP_GRACE:
                        self.closing = True
                elif self.empty_since is None:
                    self.empty_since = now
                elif now - self.empty_since >= self.EXIT_GRACE:
                    self.closing = True
                if self.closing:
                    self.stopping.set()
            for identifier in removed:
                await self.publish("ended", identifier)

    def trusted_request(self, request: Request, *, sandbox: bool = False) -> bool:
        """Reject DNS rebinding, foreign origins, and cross-site browser requests."""
        origin = self.sandbox_origin if sandbox else self.origin
        if request.headers.get("host", "") != origin.removeprefix("http://"):
            return False
        supplied_origin = request.headers.get("origin")
        if supplied_origin is not None and supplied_origin != self.origin:
            return False
        allowed_sites = ("none", "same-origin", "same-site") if sandbox else (
            "none",
            "same-origin",
        )
        return request.headers.get("sec-fetch-site", "none") in allowed_sites

    def authenticated(self, request: Request) -> bool:
        """Require the browser capability cookie for every data endpoint."""
        return secrets.compare_digest(
            request.cookies.get(self.cookie_name, ""),
            self.session_token,
        )

    async def session(self, request: Request) -> Response:
        """Exchange an OS-discovered capability for a browser-only cookie and CSRF token."""
        if not self.trusted_request(request):
            return JSONResponse({"error": "Untrusted browser origin."}, status_code=403)
        authorization = request.headers.get("authorization", "")
        if not self.authenticated(request) and not secrets.compare_digest(
            authorization,
            f"Bearer {self.bootstrap_token}",
        ):
            return JSONResponse(
                {"error": "Open the panel with: forgemcp --open-admin"},
                status_code=401,
            )
        response = JSONResponse(
            {
                "token": self.csrf_token,
                "sandbox_origin": self.sandbox_origin,
                "hub_id": self.hub_id,
            }
        )
        response.set_cookie(
            self.cookie_name,
            self.session_token,
            httponly=True,
            samesite="strict",
            path="/api",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    async def api(self, request: Request) -> Response:
        """Translate a small HTTP surface to the existing instance SDK client."""
        if not self.trusted_request(request) or not self.authenticated(request):
            return JSONResponse({"error": "Admin authentication required."}, status_code=403)
        if request.method == "POST" and not secrets.compare_digest(
            request.headers.get("x-forgemcp-token", ""),
            self.csrf_token,
        ):
            return JSONResponse({"error": "Invalid CSRF token."}, status_code=403)
        if request.url.path == "/api/instances":
            return JSONResponse(
                {"instances": [item.summary() for item in self.instances.values()]},
                headers={"Cache-Control": "no-store"},
            )
        identifier = request.path_params["instance_id"]
        instance = self.instances.get(identifier)
        if instance is None:
            return JSONResponse({"error": "This ForgeMCP session has ended."}, status_code=404)
        operation = request.path_params.get(
            "operation",
            "calls/cancel" if "call_id" in request.path_params else "snapshot",
        )
        params: dict[str, Any] = {}
        methods = {
            "snapshot": "snapshot",
            "history": "history/list",
            "state": "state/get",
            "tools": "tools/list",
            "resources": "resources/list",
            "resource-templates": "resources/templates/list",
            "elicitations": "elicitation/list",
        }
        method = methods.get(operation)
        try:
            if "call_id" in request.path_params:
                method = "calls/cancel"
                params = {"id": request.path_params["call_id"]}
            elif operation == "resources" and "uri" in request.query_params:
                method = "resources/read"
                params = dict(request.query_params)
            elif operation == "calls":
                method = "calls/get"
                params = {"id": request.path_params["item_id"]}
            elif operation == "completion" and request.method == "POST":
                method = "completion/complete"
                params = await request.json()
                if not isinstance(params, dict):
                    raise HubRequestError("Completion parameters must be a JSON object.", 400)
            elif operation == "tools" and request.method == "POST":
                body = await request.json()
                arguments = body.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise HubRequestError("Tool arguments must be a JSON object.", 400)
                method = "tools/call"
                params = {
                    "name": request.path_params["item_id"],
                    "arguments": arguments,
                }
            elif operation == "elicitations" and request.method == "POST":
                method = "elicitation/respond"
                params = {
                    "id": request.path_params["item_id"],
                    "result": (await request.json())["result"],
                }
            if method is None:
                raise HubRequestError("Unknown admin operation.", 404)
            result = await instance.request(method, params)
            if operation == "snapshot":
                result = {"instance": instance.summary(), **result}
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "Invalid request body."}, status_code=400)
        except HubRequestError as error:
            return JSONResponse({"error": str(error)}, status_code=error.status)

    async def events(self, request: Request) -> Response:
        """Stream coalescible invalidations; a slow browser never accumulates result copies."""
        if not self.trusted_request(request) or not self.authenticated(request):
            return JSONResponse({"error": "Admin authentication required."}, status_code=403)

        async def stream() -> AsyncGenerator[str]:
            """Send a revision or heartbeat until the browser disconnects or Hub stops."""
            revision = -1
            while not self.stopping.is_set():
                async with self.changed:
                    if self.revision == revision:
                        try:
                            await asyncio.wait_for(self.changed.wait(), 10)
                        except TimeoutError:
                            pass
                    next_revision = self.revision
                    event = self.last_event
                if next_revision != revision:
                    revision = next_revision
                    yield f"id: {revision}\nevent: change\ndata: {json.dumps(event)}\n\n"
                else:
                    yield ": heartbeat\n\n"
                if await request.is_disconnected():
                    break

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def page(self, request: Request) -> Response:
        """Serve only the packaged admin entrypoint, with no remotely loaded assets."""
        if not self.trusted_request(request):
            return Response(status_code=403)
        return self.html_asset("admin.html", sandbox=False)

    async def sandbox_page(self, request: Request) -> Response:
        """Serve the credential-free Apps proxy on its separate browser origin."""
        if not self.trusted_request(request, sandbox=True):
            return Response(status_code=403)
        return self.html_asset("sandbox.html", sandbox=True)

    def html_asset(self, name: str, *, sandbox: bool) -> Response:
        """Load generated package data and fail clearly when a source checkout is unbuilt."""
        asset = files("forgemcp").joinpath("assets", name)
        if not asset.is_file():
            return HTMLResponse(
                "<h1>ForgeMCP admin assets are not built</h1>"
                "<p>Build the release wheel or run the documented frontend build.</p>",
                status_code=503,
            )
        response = HTMLResponse(asset.read_text(encoding="utf-8"))
        ancestors = self.origin if sandbox else "'none'"
        frames = "'self' about:" if sandbox else self.sandbox_origin
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                    "img-src data: blob:; font-src data:; "
                    f"connect-src {' '.join((self.origin, self.sandbox_origin))}; "
                    f"frame-src {frames}; frame-ancestors {ancestors}; "
                    "base-uri 'none'; form-action 'none'"
                ),
            }
        )
        return response

    def app(self) -> Starlette:
        """Expose the panel and authenticated instance APIs through explicit routes."""
        return Starlette(
            routes=[
                Route("/", self.page),
                Route("/admin.html", self.page),
                Route("/api/session", self.session),
                Route("/api/events", self.events),
                Route("/api/instances", self.api),
                Route("/api/instances/{instance_id}/snapshot", self.api),
                Route(
                    "/api/instances/{instance_id}/calls/{call_id}/cancel",
                    self.api,
                    methods=["POST"],
                ),
                Route(
                    "/api/instances/{instance_id}/{operation}",
                    self.api,
                    methods=["GET", "POST"],
                ),
                Route(
                    "/api/instances/{instance_id}/{operation}/{item_id}",
                    self.api,
                    methods=["GET", "POST"],
                ),
            ],
        )

    @staticmethod
    def bind_socket(port: int) -> socket.socket:
        """Reserve a loopback TCP port without a bind-close race."""
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            server_socket.bind(("127.0.0.1", port))
            server_socket.listen(socket.SOMAXCONN)
            server_socket.setblocking(False)
        except BaseException:
            server_socket.close()
            raise
        return server_socket

    async def run(self) -> None:
        """Run HTTP, a separate sandbox origin, IPC, and the runtime lifetime watcher."""
        try:
            main_socket = self.bind_socket(self.port)
        except OSError:
            main_socket = self.bind_socket(0)
        sandbox_socket = self.bind_socket(0)
        self.origin = f"http://127.0.0.1:{main_socket.getsockname()[1]}"
        self.sandbox_origin = f"http://127.0.0.1:{sandbox_socket.getsockname()[1]}"
        self.url = f"{self.origin}/#token={self.bootstrap_token}"
        sandbox_app = Starlette(routes=[Route("/sandbox.html", self.sandbox_page)])
        servers = [
            uvicorn.Server(
                uvicorn.Config(
                    application,
                    access_log=False,
                    log_config=None,
                    log_level="warning",
                    lifespan="off",
                    ws="none",
                    timeout_graceful_shutdown=2,
                )
            )
            for application in (self.app(), sandbox_app)
        ]
        tasks = [
            asyncio.create_task(self.accept_connections()),
            asyncio.create_task(self.watch_lifetime()),
            asyncio.create_task(servers[0].serve(sockets=[main_socket])),
            asyncio.create_task(servers[1].serve(sockets=[sandbox_socket])),
        ]
        stop_task = asyncio.create_task(self.stopping.wait())
        try:
            await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.stopping.set()
            self.closing = True
            for server in servers:
                server.should_exit = True
            await self.listener.close()
            for connection in tuple(self.connections):
                await connection.close()
            for task in [*tasks, *self.tasks, stop_task]:
                task.cancel()
            for task in [*tasks, *self.tasks, stop_task]:
                with suppress(asyncio.CancelledError, local_ipc.IpcError):
                    await task
            main_socket.close()
            sandbox_socket.close()


async def discover_url(ipc_name: str) -> str:
    """Read the browser capability through the OS-protected channel for --open-admin."""
    connection = await local_ipc.connect(ipc_name)
    try:
        await connection.send(b'{"type":"discover"}')
        async with asyncio.timeout(5):
            payload = await connection.receive()
        if payload is None:
            raise HubRequestError("The admin Hub is shutting down. Try again.")
        message = json.loads(payload)
        if message.get("type") != "discovered":
            raise HubRequestError("The local endpoint is not a compatible admin Hub.")
        return str(message["url"])
    finally:
        await connection.close()
