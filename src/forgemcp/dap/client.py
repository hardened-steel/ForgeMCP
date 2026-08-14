"""Concurrent, bounded client transport for one Debug Adapter Protocol session."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum

from forgemcp.dap.errors import (
    DapConnectionClosedError,
    DapError,
    DapProtocolError,
    DapRequestError,
    DapRequestTimeoutError,
)
from forgemcp.dap.framing import MAX_DAP_MESSAGE_BYTES, frame_message, read_message
from forgemcp.dap.protocol import DapEvent, DapResponse, DapReverseRequest, parse_message

DapEventHandler = Callable[[str, Mapping[str, object]], object | Awaitable[object]]
_MAX_QUEUED_EVENTS = 16
_MAX_QUEUED_REVERSE_REQUESTS = 8


class DapClientState(StrEnum):
    """Lifecycle state of a :class:`DapClient`."""

    CREATED = "created"
    RUNNING = "running"
    FAILED = "failed"
    CLOSED = "closed"


class DapClient:
    """Route DAP responses by ``request_seq`` while safely denying reverse requests.

    The class has only asyncio stream dependencies.  It intentionally does not
    own a subprocess, ProcessHandle, workspace, logger, or MCP connection.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        event_handler: DapEventHandler | None = None,
        default_timeout_seconds: float = 15.0,
        max_message_bytes: int = MAX_DAP_MESSAGE_BYTES,
    ) -> None:
        if not isinstance(max_message_bytes, int) or isinstance(max_message_bytes, bool) or max_message_bytes < 1:
            raise ValueError("max_message_bytes must be a positive integer.")
        if isinstance(default_timeout_seconds, bool) or not isinstance(default_timeout_seconds, (int, float)) or default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be greater than zero.")
        self._reader = reader
        self._writer = writer
        self._event_handler = event_handler
        self._max_message_bytes = max_message_bytes
        self._default_timeout_seconds = float(default_timeout_seconds)
        self._state = DapClientState.CREATED
        self._reader_task: asyncio.Task[None] | None = None
        # Each validated inbound body can still approach the 1 MiB framing
        # limit.  Keep the temporary pre-normalization queues deliberately
        # small, then fail and close rather than retaining an adapter flood.
        self._event_queue: asyncio.Queue[DapEvent] = asyncio.Queue(maxsize=_MAX_QUEUED_EVENTS)
        self._reverse_queue: asyncio.Queue[DapReverseRequest] = asyncio.Queue(maxsize=_MAX_QUEUED_REVERSE_REQUESTS)
        self._event_task: asyncio.Task[None] | None = None
        self._reverse_tasks: set[asyncio.Task[None]] = set()
        self._pending: dict[int, tuple[str, asyncio.Future[DapResponse]]] = {}
        self._next_seq = 1
        self._write_lock = asyncio.Lock()
        self._failure: DapError | None = None
        self._supports_cancel_request = False
        self._expected_eof = False
        self._closing = False
        self._late_responses = 0

    @property
    def state(self) -> DapClientState:
        """Return the current transport lifecycle state."""
        return self._state

    @property
    def failure(self) -> DapError | None:
        """Return the safe terminal failure, if the stream failed."""
        return self._failure

    @property
    def late_response_count(self) -> int:
        """Return the number of harmless late/unknown adapter responses."""
        return self._late_responses

    @property
    def supports_cancel_request(self) -> bool:
        """Whether ``initialize`` explicitly advertised DAP request cancellation."""
        return self._supports_cancel_request

    async def start(self) -> None:
        """Create the single reader task once."""
        if self._state is DapClientState.RUNNING:
            return
        if self._state is not DapClientState.CREATED:
            raise DapConnectionClosedError("The DAP client cannot be restarted after close or failure.")
        self._state = DapClientState.RUNNING
        self._reader_task = asyncio.create_task(self._read_loop(), name="forgemcp-dap-reader")
        if self._event_handler is not None:
            self._event_task = asyncio.create_task(self._event_loop(), name="forgemcp-dap-events")
        self._reverse_tasks = {
            asyncio.create_task(self._reverse_loop(), name=f"forgemcp-dap-reverse-{index}")
            for index in range(4)
        }

    async def request(
        self,
        command: str,
        arguments: Mapping[str, object] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        """Send a DAP request and return its successful object body.

        Caller cancellation and deadlines request DAP cancellation only after
        the adapter opted into that capability during ``initialize``.
        """
        self._require_running()
        self._validate_command(command)
        timeout = self._validate_timeout(timeout_seconds)
        sequence = self._allocate_sequence()
        future: asyncio.Future[DapResponse] = asyncio.get_running_loop().create_future()
        self._pending[sequence] = (command, future)
        try:
            payload: dict[str, object] = {"seq": sequence, "type": "request", "command": command}
            if arguments is not None:
                payload["arguments"] = dict(arguments)
            await self._send(payload)
            try:
                response = await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
            except TimeoutError as error:
                await self._cancel_pending(sequence)
                raise DapRequestTimeoutError("The debug adapter did not answer before the request timeout.") from error
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(self._cancel_pending(sequence))
                # ``shield`` lets the best-effort protocol cleanup finish even
                # though this caller must still observe its cancellation.
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup)
                raise
            if not response.success:
                raise DapRequestError(command, response.message)
            if command == "initialize":
                self._supports_cancel_request = response.body.get("supportsCancelRequest") is True
            return response.body
        finally:
            pending = self._pending.pop(sequence, None)
            # A failed write can fail this private future before the caller
            # has awaited it.  Consume that exception so asyncio never emits
            # a payload-adjacent "Future exception was never retrieved"
            # diagnostic during teardown.
            if pending is not None and pending[1].done() and not pending[1].cancelled():
                with contextlib.suppress(DapError):
                    pending[1].exception()

    async def cancel(self, request_seq: int) -> None:
        """Best-effort DAP cancellation, only when explicitly supported."""
        if not isinstance(request_seq, int) or isinstance(request_seq, bool) or request_seq <= 0:
            raise ValueError("request_seq must be a positive integer.")
        if self._state is not DapClientState.RUNNING or not self._supports_cancel_request:
            return
        with contextlib.suppress(DapError):
            await self._send({"seq": self._allocate_sequence(), "type": "request", "command": "cancel", "arguments": {"requestId": request_seq}})

    async def aclose(self, *, expected_eof: bool = False) -> None:
        """Fail all pending operations and close transport resources idempotently."""
        if self._closing:
            return
        self._closing = True
        self._expected_eof = expected_eof
        if self._state not in {DapClientState.FAILED, DapClientState.CLOSED}:
            self._state = DapClientState.CLOSED
            self._fail_pending(DapConnectionClosedError("The DAP client is closing."))
        current_task = asyncio.current_task()
        if self._reader_task is not None and self._reader_task is not current_task:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        tasks = tuple(task for task in (self._event_task, *self._reverse_tasks) if task is not None)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._event_task = None
        self._reverse_tasks.clear()
        if not self._writer.is_closing():
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()

    async def _read_loop(self) -> None:
        try:
            while self._state is DapClientState.RUNNING:
                await self._dispatch(parse_message(await read_message(self._reader, max_message_bytes=self._max_message_bytes)))
        except asyncio.CancelledError:
            raise
        except asyncio.IncompleteReadError as error:
            if not self._expected_eof and not self._closing:
                self._fail(DapConnectionClosedError("The debug adapter closed its protocol stream."))
        except DapError as error:
            self._fail(error)
        except Exception:
            self._fail(DapProtocolError("The debug-adapter protocol stream failed."))
        finally:
            # A malformed peer is not left with a live stdin and background
            # handler workers.  ``aclose`` knows not to await this reader from
            # itself, so this remains safe on the reader task.
            if self._state is DapClientState.FAILED:
                await self.aclose()

    async def _dispatch(self, message: DapResponse | DapEvent | DapReverseRequest) -> None:
        if isinstance(message, DapResponse):
            pending = self._pending.get(message.request_seq)
            if pending is None or pending[1].done():
                self._late_responses += 1
            elif pending[0] != message.command:
                self._fail(DapProtocolError("The debug adapter sent a response for the wrong command."))
            else:
                pending[1].set_result(message)
            return
        if isinstance(message, DapEvent):
            if self._event_handler is not None:
                try:
                    self._event_queue.put_nowait(message)
                except asyncio.QueueFull:
                    self._fail(DapProtocolError("The debug adapter exceeded the bounded event queue."))
            return
        try:
            self._reverse_queue.put_nowait(message)
        except asyncio.QueueFull:
            self._fail(DapProtocolError("The debug adapter exceeded the bounded reverse-request queue."))

    async def _event_loop(self) -> None:
        while True:
            await self._publish_event(await self._event_queue.get())

    async def _reverse_loop(self) -> None:
        while True:
            await self._deny_reverse_request(await self._reverse_queue.get())

    async def _publish_event(self, event: DapEvent) -> None:
        try:
            result = self._event_handler(event.event, event.body)  # type: ignore[misc]
            if inspect.isawaitable(result):
                await result
        except Exception:
            # An event consumer cannot destabilize byte framing or pending requests.
            return

    async def _deny_reverse_request(self, request: DapReverseRequest) -> None:
        # The service deliberately has no terminal broker or nested-session
        # launcher.  Unknown commands are equally denied; their arguments are
        # never interpreted or logged.
        message = "Reverse DAP requests are not supported by ForgeMCP."
        if request.command == "runInTerminal":
            message = "runInTerminal is disabled by ForgeMCP policy."
        elif request.command == "startDebugging":
            message = "startDebugging is disabled by ForgeMCP policy."
        try:
            await asyncio.wait_for(
                self._send({
                    "seq": self._allocate_sequence(),
                    "type": "response",
                    "request_seq": request.seq,
                    "success": False,
                    "command": request.command,
                    "message": message,
                }),
                timeout=10.0,
            )
        except (TimeoutError, DapError):
            return

    async def _cancel_pending(self, sequence: int) -> None:
        self._pending.pop(sequence, None)
        await self.cancel(sequence)

    async def _send(self, message: Mapping[str, object]) -> None:
        self._require_running()
        framed = frame_message(message, max_message_bytes=self._max_message_bytes)
        async with self._write_lock:
            if self._writer.is_closing():
                error = DapConnectionClosedError("The debug-adapter protocol stream is closed.")
                self._fail(error)
                raise error
            self._writer.write(framed)
            try:
                await self._writer.drain()
            except (ConnectionError, BrokenPipeError) as error:
                closed = DapConnectionClosedError("The debug-adapter protocol stream is closed.")
                self._fail(closed)
                raise closed from error

    def _allocate_sequence(self) -> int:
        sequence = self._next_seq
        self._next_seq += 1
        return sequence

    def _require_running(self) -> None:
        if self._state is not DapClientState.RUNNING:
            raise self._failure or DapConnectionClosedError("The DAP client is not running.")

    def _validate_timeout(self, value: float | None) -> float:
        timeout = self._default_timeout_seconds if value is None else value
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("DAP request timeout must be greater than zero.")
        return float(timeout)

    @staticmethod
    def _validate_command(command: str) -> None:
        if not isinstance(command, str) or not command or len(command) > 256 or "\x00" in command:
            raise ValueError("DAP commands must be bounded non-empty NUL-free strings.")

    def _fail(self, error: DapError) -> None:
        if self._state in {DapClientState.FAILED, DapClientState.CLOSED}:
            return
        self._state = DapClientState.FAILED
        self._failure = error
        self._fail_pending(error)
        if not self._writer.is_closing():
            self._writer.close()

    def _fail_pending(self, error: DapError) -> None:
        for _, future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)
