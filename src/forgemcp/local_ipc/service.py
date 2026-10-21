"""Byte framing shared by private Windows pipes and Unix sockets."""

from __future__ import annotations

import asyncio
import os
import re
import struct

from .errors import IpcClosedError, IpcError, IpcProtocolError

if os.name == "nt":
    from . import windows as backend
else:
    from . import unix as backend


class IpcConnection:
    """Exchange complete byte frames with one same-user local peer.

    Read and write may run concurrently. Cancellation inside a frame closes the
    connection, because its remaining bytes cannot safely form another frame.
    """

    MAX_FRAME_BYTES = 0xFFFFFFFF
    READ_CHUNK_BYTES = 65536

    def __init__(self, transport: backend.Connection) -> None:
        """Own an already authorized native connection and its frame locks."""
        self.transport = transport
        self.send_lock = asyncio.Lock()
        self.receive_lock = asyncio.Lock()
        self.closed = False
        self.close_task: asyncio.Task[None] | None = None

    @property
    def peer_pid(self) -> int | None:
        """Return the kernel-reported peer PID when the platform supplies one."""
        return self.transport.peer_pid

    async def send(self, payload: bytes) -> None:
        """Write one uint32-length-prefixed frame without interleaving senders."""
        if not isinstance(payload, bytes):
            raise TypeError("IPC payload must be bytes.")
        if len(payload) > self.MAX_FRAME_BYTES:
            raise IpcProtocolError("IPC frame exceeds the uint32 length limit.")
        async with self.send_lock:
            if self.closed:
                raise IpcClosedError("IPC connection is closed.")
            try:
                await self.transport.write(struct.pack("!I", len(payload)))
                if payload:
                    await self.transport.write(payload)
            except BaseException:
                await self.close()
                raise

    async def receive(self) -> bytes | None:
        """Read one frame, returning None only for EOF between complete frames."""
        async with self.receive_lock:
            if self.closed:
                raise IpcClosedError("IPC connection is closed.")
            try:
                header = await self.read_exactly(4, boundary=True)
                if header is None:
                    await self.close()
                    return None
                length = struct.unpack("!I", header)[0]
                return await self.read_exactly(length)
            except BaseException:
                await self.close()
                raise

    async def read_exactly(
        self,
        length: int,
        *,
        boundary: bool = False,
    ) -> bytes | None:
        """Accumulate bounded reads without allocating the peer's advertised size upfront."""
        chunks = bytearray()
        while len(chunks) < length:
            data = await self.transport.read(
                min(length - len(chunks), self.READ_CHUNK_BYTES)
            )
            if not data:
                if boundary and not chunks:
                    return None
                raise IpcProtocolError("Peer disconnected inside an IPC frame.")
            chunks.extend(data)
        return bytes(chunks)

    async def close(self) -> None:
        """Idempotently close native I/O and finish cleanup even under cancellation."""
        self.closed = True
        if self.close_task is None:
            self.close_task = asyncio.create_task(self.transport.close())
        await finish_cleanup(self.close_task)


class IpcListener:
    """Accept authorized local peers while retaining exclusive endpoint ownership."""

    def __init__(self, transport: backend.Listener) -> None:
        """Own a native listener independently of its accepted connections."""
        self.transport = transport
        self.accept_lock = asyncio.Lock()
        self.closed = False
        self.close_task: asyncio.Task[None] | None = None

    async def accept(self) -> IpcConnection:
        """Accept one peer; cancelling this wait leaves the listener usable."""
        async with self.accept_lock:
            if self.closed:
                raise IpcClosedError("IPC listener is closed.")
            return IpcConnection(await self.transport.accept())

    async def close(self) -> None:
        """Stop pending accepts and release endpoint ownership exactly once."""
        self.closed = True
        if self.close_task is None:
            self.close_task = asyncio.create_task(self.transport.close())
        await finish_cleanup(self.close_task)


async def finish_cleanup(task: asyncio.Task[None]) -> None:
    """Wait for resource cleanup without allowing cancellation to abandon its task."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


def validate_name(name: str) -> None:
    """Accept bounded logical names rather than paths or remote pipe addresses."""
    if (
        not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", name) is None
    ):
        raise IpcError(
            "IPC name must contain 1-96 ASCII letters, digits, dots, dashes, or underscores."
        )


async def connect(name: str) -> IpcConnection:
    """Connect to the current user's logical endpoint and verify its owner."""
    validate_name(name)
    return IpcConnection(await backend.connect(name))


async def listen(name: str) -> IpcListener:
    """Claim a current-user endpoint or fail if another listener owns it."""
    validate_name(name)
    return IpcListener(await backend.listen(name))
