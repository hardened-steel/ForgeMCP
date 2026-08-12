"""A small, transport-neutral JSON-RPC 2.0 client for LSP stdio streams."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from typing import Any

from forgemcp.lsp.errors import (
    LspConnectionClosedError,
    LspError,
    LspProtocolError,
    LspRequestTimeoutError,
    LspRpcError,
)

JsonRpcMessageHandler = Callable[[str, Mapping[str, object]], object | Awaitable[object]]


class LspClientState(StrEnum):
    """Lifecycle state of an :class:`LspClient`."""

    CREATED = "created"
    RUNNING = "running"
    FAILED = "failed"
    CLOSED = "closed"


class LspClient:
    """Multiplex ordered writes and out-of-order JSON-RPC responses over LSP streams.

    The owner supplies byte streams, usually from ``ProcessHandle``.  This
    class deliberately has no Process Runtime or MCP dependency, so it can be
    tested with deterministic in-memory streams and reused by another managed
    language-server adapter.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        max_message_bytes: int = 4 * 1024 * 1024,
        default_timeout_seconds: float = 15.0,
        notification_handler: JsonRpcMessageHandler | None = None,
    ) -> None:
        if not isinstance(max_message_bytes, int) or isinstance(max_message_bytes, bool) or max_message_bytes < 1:
            raise ValueError("max_message_bytes must be a positive integer.")
        if (
            isinstance(default_timeout_seconds, bool)
            or not isinstance(default_timeout_seconds, (int, float))
            or default_timeout_seconds <= 0
        ):
            raise ValueError("default_timeout_seconds must be greater than zero.")
        self._reader = reader
        self._writer = writer
        self._max_message_bytes = max_message_bytes
        self._default_timeout_seconds = float(default_timeout_seconds)
        self._notification_handler = notification_handler
        self._state = LspClientState.CREATED
        self._reader_task: asyncio.Task[None] | None = None
        self._server_request_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[int, asyncio.Future[object]] = {}
        self._next_request_id = 1
        self._write_lock = asyncio.Lock()
        self._failure: LspError | None = None

    @property
    def state(self) -> LspClientState:
        """Return the client lifecycle state."""
        return self._state

    async def start(self) -> None:
        """Start the single reader task exactly once."""
        if self._state is LspClientState.RUNNING:
            return
        if self._state is not LspClientState.CREATED:
            raise LspConnectionClosedError("The LSP client cannot be restarted after it closes or fails.")
        self._state = LspClientState.RUNNING
        self._reader_task = asyncio.create_task(self._read_loop(), name="forgemcp-lsp-reader")

    async def request(
        self, method: str, params: Mapping[str, object] | None = None, *, timeout_seconds: float | None = None
    ) -> object:
        """Send a JSON-RPC request and await its matching response.

        Cancellation and timeout send the standard ``$/cancelRequest``
        notification before the local pending entry is removed.
        """
        self._require_running()
        if not isinstance(method, str) or not method:
            raise ValueError("JSON-RPC methods must be non-empty strings.")
        timeout = self._validate_timeout(timeout_seconds)
        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[object] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            message: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = dict(params)
            await self._send_message(message)
            try:
                return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except TimeoutError as error:
                await self._cancel_request(request_id)
                raise LspRequestTimeoutError("The language server did not answer before the request timeout.") from error
            except asyncio.CancelledError:
                await asyncio.shield(self._cancel_request(request_id))
                raise
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, object] | None = None) -> None:
        """Send a JSON-RPC notification without creating a pending request."""
        self._require_running()
        if not isinstance(method, str) or not method:
            raise ValueError("JSON-RPC methods must be non-empty strings.")
        message: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        await self._send_message(message)

    async def aclose(self) -> None:
        """Fail pending requests and stop reader/server-request tasks idempotently."""
        if self._state is LspClientState.CLOSED:
            return
        self._state = LspClientState.CLOSED
        self._fail_pending(LspConnectionClosedError("The LSP client is closing."))
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        for task in tuple(self._server_request_tasks):
            task.cancel()
        if self._server_request_tasks:
            await asyncio.gather(*self._server_request_tasks, return_exceptions=True)
        if not self._writer.is_closing():
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()

    async def _read_loop(self) -> None:
        try:
            while self._state is LspClientState.RUNNING:
                message = await self._read_message()
                await self._dispatch_message(message)
        except asyncio.CancelledError:
            raise
        except asyncio.IncompleteReadError as error:
            self._fail(LspConnectionClosedError("The language server closed its protocol stream."), error)
        except LspError as error:
            self._fail(error, error)
        except Exception as error:  # Defensive boundary: no protocol payload reaches callers or logs.
            self._fail(LspProtocolError("The language-server protocol stream failed."), error)

    async def _read_message(self) -> Mapping[str, object]:
        try:
            headers = await self._reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError as error:
            raise LspProtocolError("The language-server message headers exceed the configured limit.") from error
        if len(headers) > 16 * 1024:
            raise LspProtocolError("The language-server message headers exceed the configured limit.")
        content_length: int | None = None
        for raw_line in headers[:-4].split(b"\r\n"):
            try:
                name, raw_value = raw_line.split(b":", 1)
            except ValueError as error:
                raise LspProtocolError("The language server sent malformed message headers.") from error
            if name.strip().lower() == b"content-length":
                if content_length is not None:
                    raise LspProtocolError("The language server sent duplicate Content-Length headers.")
                try:
                    content_length = int(raw_value.strip())
                except ValueError as error:
                    raise LspProtocolError("The language server sent an invalid Content-Length header.") from error
        if content_length is None or content_length < 0 or content_length > self._max_message_bytes:
            raise LspProtocolError("The language server sent a message outside the configured size limit.")
        payload = await self._reader.readexactly(content_length)
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LspProtocolError("The language server sent malformed JSON-RPC data.") from error
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise LspProtocolError("The language server sent an invalid JSON-RPC message.")
        return value

    async def _dispatch_message(self, message: Mapping[str, object]) -> None:
        method = message.get("method")
        if isinstance(method, str):
            params = message.get("params", {})
            if not isinstance(params, Mapping):
                raise LspProtocolError("The language server sent non-object request parameters.")
            if "id" in message:
                if not isinstance(message["id"], (int, str)) or isinstance(message["id"], bool):
                    raise LspProtocolError("The language server sent an invalid request id.")
                task = asyncio.create_task(self._answer_server_request(message["id"], method, params))
                self._server_request_tasks.add(task)
                task.add_done_callback(self._server_request_tasks.discard)
            elif self._notification_handler is not None:
                result = self._notification_handler(method, params)
                if inspect.isawaitable(result):
                    await result
            return
        response_id = message.get("id")
        if not isinstance(response_id, int) or isinstance(response_id, bool):
            raise LspProtocolError("The language server sent a response without a numeric request id.")
        future = self._pending.get(response_id)
        if future is None or future.done():
            return  # Late or unknown responses are safe to ignore.
        if "error" in message:
            error = message["error"]
            code = error.get("code") if isinstance(error, Mapping) else None
            future.set_exception(LspRpcError(f"The language server rejected a request (code {code!s})."))
        elif "result" in message:
            future.set_result(message["result"])
        else:
            future.set_exception(LspProtocolError("The language server sent an invalid response."))

    async def _answer_server_request(
        self, request_id: object, method: str, params: Mapping[str, object]
    ) -> None:
        try:
            if method == "workspace/configuration":
                items = params.get("items", [])
                result: object = [{} for _ in items] if isinstance(items, list) else []
            elif method == "workspace/applyEdit":
                result = {"applied": False}
            elif method in {"window/workDoneProgress/create", "client/registerCapability", "client/unregisterCapability"}:
                result = None
            else:
                await self._send_message(
                    {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not supported"}}
                )
                return
            await self._send_message({"jsonrpc": "2.0", "id": request_id, "result": result})
        except LspError as error:
            self._fail(error, error)

    async def _send_message(self, message: Mapping[str, object]) -> None:
        try:
            body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise LspProtocolError("ForgeMCP could not encode a JSON-RPC message.") from error
        if len(body) > self._max_message_bytes:
            raise LspProtocolError("The outbound language-server message exceeds the configured size limit.")
        framed = b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
        async with self._write_lock:
            if self._writer.is_closing():
                raise LspConnectionClosedError("The language-server protocol stream is closed.")
            self._writer.write(framed)
            try:
                await self._writer.drain()
            except (ConnectionError, BrokenPipeError) as error:
                raise LspConnectionClosedError("The language-server protocol stream is closed.") from error

    async def _cancel_request(self, request_id: int) -> None:
        if self._state is LspClientState.RUNNING:
            with contextlib.suppress(LspError):
                await self.notify("$/cancelRequest", {"id": request_id})

    def _require_running(self) -> None:
        if self._state is not LspClientState.RUNNING:
            raise self._failure or LspConnectionClosedError("The LSP client is not running.")

    def _validate_timeout(self, timeout_seconds: float | None) -> float:
        value = self._default_timeout_seconds if timeout_seconds is None else timeout_seconds
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError("LSP request timeout must be greater than zero.")
        return float(value)

    def _fail(self, error: LspError, _: BaseException) -> None:
        if self._state in {LspClientState.CLOSED, LspClientState.FAILED}:
            return
        self._state = LspClientState.FAILED
        self._failure = error
        self._fail_pending(error)

    def _fail_pending(self, error: LspError) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
