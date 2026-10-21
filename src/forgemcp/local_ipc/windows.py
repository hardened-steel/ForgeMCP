"""Overlapped Windows named pipes restricted to the current local user."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import hashlib

import pywintypes
import win32api
import win32con
import win32event
import win32file
import win32pipe
import win32security
import winerror

from .errors import (
    IpcAddressInUseError,
    IpcClosedError,
    IpcError,
    IpcUnavailableError,
)


def current_sid() -> pywintypes.SID:
    """Read the current process token's user SID without impersonating a peer."""
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(),
        win32con.TOKEN_QUERY,
    )
    try:
        return win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()


def pipe_name(name: str) -> str:
    """Derive a bounded pipe namespace shared only by one user and logical name."""
    sid = win32security.ConvertSidToStringSid(current_sid())
    scope = hashlib.sha256(sid.encode("ascii")).hexdigest()[:24]
    return rf"\\.\pipe\forgemcp-{scope}-{name}"


def security_attributes() -> pywintypes.SECURITY_ATTRIBUTES:
    """Build an explicit non-inherited DACL granting access only to the current SID."""
    sid = current_sid()
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        sid,
    )
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorOwner(sid, False)
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    attributes = pywintypes.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


def verify_peer(handle: pywintypes.HANDLE, *, server: bool) -> int:
    """Verify the kernel-reported peer process has the current user's primary token."""
    try:
        pid = (
            win32pipe.GetNamedPipeServerProcessId(handle)
            if server else win32pipe.GetNamedPipeClientProcessId(handle)
        )
        process = win32api.OpenProcess(0x1000, False, pid)
        try:
            token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
            try:
                sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
            finally:
                token.Close()
        finally:
            process.Close()
        if sid != current_sid():
            raise IpcError("IPC peer belongs to another user.")
        return pid
    except pywintypes.error as error:
        raise IpcError("Cannot verify the IPC peer's identity.") from error


class Connection:
    """Own a duplex pipe handle and complete every overlapped operation before closing it."""

    POLL_INTERVAL = 0.01
    CHUNK_BYTES = 65536
    REJECT_REMOTE_CLIENTS = 0x00000008
    FIRST_PIPE_INSTANCE = 0x00080000
    SECURITY_SQOS_PRESENT = 0x00100000
    SECURITY_IDENTIFICATION = 0x00010000

    def __init__(
        self,
        handle: pywintypes.HANDLE,
        peer_pid: int | None = None,
    ) -> None:
        """Retain an overlapped handle and count I/O that still owns native buffers."""
        self.handle = handle
        self.peer_pid = peer_pid
        self.closed = False
        self.pending = 0
        self.idle = asyncio.Event()
        self.idle.set()

    @asynccontextmanager
    async def operation(self) -> AsyncGenerator[pywintypes.OVERLAPPED, None]:
        """Keep an event and handle alive until an issued operation has completed."""
        if self.closed:
            raise IpcClosedError("IPC pipe is closed.")
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        self.pending += 1
        self.idle.clear()
        try:
            yield overlapped
        finally:
            overlapped.hEvent.Close()
            self.pending -= 1
            if not self.pending:
                self.idle.set()

    async def wait_event(self, overlapped: pywintypes.OVERLAPPED) -> None:
        """Poll an overlapped event cooperatively without occupying executor threads."""
        while win32event.WaitForSingleObject(overlapped.hEvent, 0) == win32event.WAIT_TIMEOUT:
            await asyncio.sleep(self.POLL_INTERVAL)

    async def complete(
        self,
        overlapped: pywintypes.OVERLAPPED,
        status: int | None,
    ) -> int:
        """Finish native I/O, draining a cancelled operation before releasing its buffers."""
        if status == winerror.ERROR_IO_PENDING:
            try:
                await self.wait_event(overlapped)
            except asyncio.CancelledError:
                win32file.CancelIo(self.handle)
                cleanup = asyncio.create_task(self.wait_event(overlapped))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
                try:
                    win32file.GetOverlappedResult(self.handle, overlapped, False)
                except pywintypes.error:
                    pass
                raise
        return win32file.GetOverlappedResult(self.handle, overlapped, False)

    async def read(self, size: int) -> bytes:
        """Read up to size bytes; broken pipes are clean native EOF."""
        try:
            async with self.operation() as overlapped:
                status, buffer = win32file.ReadFile(self.handle, size, overlapped)
                count = await self.complete(overlapped, status)
                return bytes(buffer[:count])
        except pywintypes.error as error:
            if error.winerror in (
                winerror.ERROR_BROKEN_PIPE,
                winerror.ERROR_NO_DATA,
                winerror.ERROR_PIPE_NOT_CONNECTED,
            ):
                return b""
            raise IpcClosedError("IPC pipe read failed.") from error

    async def write(self, data: bytes) -> None:
        """Write all bytes in bounded overlapped transfers with native backpressure."""
        try:
            offset = 0
            while offset < len(data):
                chunk = memoryview(data)[offset:offset + self.CHUNK_BYTES]
                async with self.operation() as overlapped:
                    status, _ = win32file.WriteFile(self.handle, chunk, overlapped)
                    count = await self.complete(overlapped, status)
                    if not count:
                        raise IpcClosedError("IPC pipe write made no progress.")
                    offset += count
        except pywintypes.error as error:
            raise IpcClosedError("IPC pipe write failed.") from error

    async def accept(self) -> None:
        """Wait for a client, including the normal connect-before-accept race."""
        try:
            async with self.operation() as overlapped:
                try:
                    status = win32pipe.ConnectNamedPipe(self.handle, overlapped)
                except pywintypes.error as error:
                    if error.winerror == winerror.ERROR_PIPE_CONNECTED:
                        return
                    if error.winerror != winerror.ERROR_IO_PENDING:
                        raise
                    status = winerror.ERROR_IO_PENDING
                if status == winerror.ERROR_PIPE_CONNECTED:
                    return
                await self.complete(overlapped, status)
        except pywintypes.error as error:
            raise IpcClosedError("IPC pipe accept failed.") from error

    async def close(self) -> None:
        """Cancel pending I/O, wait for its completion, and close the native handle."""
        if self.closed:
            await self.idle.wait()
            return
        self.closed = True
        if self.pending:
            win32file.CancelIo(self.handle)
            await self.idle.wait()
        self.handle.Close()


def create_pipe(path: str, *, owner: bool = False) -> pywintypes.HANDLE:
    """Create a private local duplex pipe, optionally claiming exclusive listener ownership."""
    flags = win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED
    if owner:
        flags |= Connection.FIRST_PIPE_INSTANCE
    return win32pipe.CreateNamedPipe(
        path,
        flags,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | Connection.REJECT_REMOTE_CLIENTS,
        1 if owner else win32pipe.PIPE_UNLIMITED_INSTANCES,
        Connection.CHUNK_BYTES,
        Connection.CHUNK_BYTES,
        0,
        security_attributes(),
    )


class Listener:
    """Retain a private ownership pipe separately from the active accept pipe."""

    def __init__(
        self,
        path: str,
        owner: pywintypes.HANDLE,
        pending: Connection,
    ) -> None:
        """Keep ownership alive even when every data connection has closed."""
        self.path = path
        self.owner = owner
        self.pending = pending
        self.closed = False

    async def accept(self) -> Connection:
        """Authorize one client and immediately replenish the available pipe instance."""
        pending = self.pending
        try:
            await pending.accept()
            if self.closed:
                raise IpcClosedError("IPC listener is closed.")
            pending.peer_pid = verify_peer(pending.handle, server=False)
            self.pending = Connection(create_pipe(self.path))
            return pending
        except BaseException:
            await pending.close()
            if not self.closed:
                self.pending = Connection(create_pipe(self.path))
            raise

    async def close(self) -> None:
        """Stop accepting without taking ownership of previously accepted connections."""
        if not self.closed:
            self.closed = True
            await self.pending.close()
            self.owner.Close()


async def connect(name: str) -> Connection:
    """Open an overlapped client and verify the named-pipe server's user."""
    try:
        handle = win32file.CreateFile(
            pipe_name(name) + ".data",
            win32con.GENERIC_READ | win32con.GENERIC_WRITE,
            0,
            None,
            win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OVERLAPPED
            | Connection.SECURITY_SQOS_PRESENT
            | Connection.SECURITY_IDENTIFICATION,
            None,
        )
    except pywintypes.error as error:
        if error.winerror in (
            winerror.ERROR_FILE_NOT_FOUND,
            winerror.ERROR_PATH_NOT_FOUND,
            winerror.ERROR_PIPE_BUSY,
        ):
            raise IpcUnavailableError("IPC endpoint is unavailable.") from error
        raise IpcError("Cannot open the IPC endpoint.") from error
    try:
        return Connection(handle, verify_peer(handle, server=True))
    except BaseException:
        handle.Close()
        raise


async def listen(name: str) -> Listener:
    """Elect one listener using a retained FIRST_PIPE_INSTANCE ownership handle."""
    path = pipe_name(name)
    try:
        owner = create_pipe(path + ".owner", owner=True)
    except pywintypes.error as error:
        if error.winerror in (winerror.ERROR_ACCESS_DENIED, winerror.ERROR_PIPE_BUSY):
            raise IpcAddressInUseError("IPC endpoint already has a listener.") from error
        raise IpcError("Cannot claim the IPC endpoint.") from error
    try:
        return Listener(path + ".data", owner, Connection(create_pipe(path + ".data")))
    except BaseException:
        owner.Close()
        raise
