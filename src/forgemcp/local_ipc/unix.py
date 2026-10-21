"""Private Unix sockets with kernel peer checks and crash-safe listener ownership."""

from __future__ import annotations

import asyncio
import ctypes
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile

from .errors import (
    IpcAddressInUseError,
    IpcClosedError,
    IpcError,
    IpcUnavailableError,
)


def endpoint_path(name: str) -> Path:
    """Locate a short socket path inside a verified private per-user directory."""
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()).resolve()
    directory = base / f"forgemcp-ipc-{os.geteuid()}"
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise IpcError("IPC runtime directory must be private and owned by the current user.")
    except OSError as error:
        raise IpcError("Cannot access the private IPC runtime directory.") from error
    digest = hashlib.sha256(name.encode("ascii")).hexdigest()[:32]
    return directory / f"{digest}.sock"


def verify_peer(peer: socket.socket) -> int | None:
    """Verify the kernel-reported effective user on Linux and BSD-derived systems."""
    if hasattr(socket, "SO_PEERCRED"):
        pid, uid, _ = struct.unpack(
            "3i",
            peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
        )
    else:
        library = ctypes.CDLL(None, use_errno=True)
        try:
            function = library.getpeereid
        except AttributeError as error:
            raise IpcError("The platform does not provide local peer credentials.") from error
        function.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        function.restype = ctypes.c_int
        user = ctypes.c_uint()
        group = ctypes.c_uint()
        if function(peer.fileno(), ctypes.byref(user), ctypes.byref(group)) != 0:
            raise IpcError("Cannot verify the IPC peer's identity.")
        uid = user.value
        pid = None
    if uid != os.geteuid():
        raise IpcError("IPC peer belongs to another user.")
    return pid


class Connection:
    """Own a nonblocking Unix socket and cancel its pending operations on close."""

    def __init__(self, peer: socket.socket, peer_pid: int | None) -> None:
        """Retain a verified peer socket and its active asyncio operations."""
        self.socket = peer
        self.peer_pid = peer_pid
        self.closed = False
        self.pending: set[asyncio.Task] = set()

    async def read(self, size: int) -> bytes:
        """Receive a bounded chunk while retaining a cancellable native operation."""
        if self.closed:
            raise IpcClosedError("IPC socket is closed.")
        task = asyncio.create_task(
            asyncio.get_running_loop().sock_recv(self.socket, size)
        )
        self.pending.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            if self.closed:
                raise IpcClosedError("IPC socket is closed.") from None
            raise
        except OSError as error:
            raise IpcClosedError("IPC socket read failed.") from error
        finally:
            self.pending.discard(task)

    async def write(self, data: bytes) -> None:
        """Send the complete bytes with socket backpressure and no blocking calls."""
        if self.closed:
            raise IpcClosedError("IPC socket is closed.")
        task = asyncio.create_task(
            asyncio.get_running_loop().sock_sendall(self.socket, data)
        )
        self.pending.add(task)
        try:
            await task
        except asyncio.CancelledError:
            if self.closed:
                raise IpcClosedError("IPC socket is closed.") from None
            raise
        except OSError as error:
            raise IpcClosedError("IPC socket write failed.") from error
        finally:
            self.pending.discard(task)

    async def close(self) -> None:
        """Cancel active socket waits before releasing the file descriptor."""
        if not self.closed:
            self.closed = True
            tasks = tuple(self.pending)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.socket.close()


class Listener:
    """Hold the Unix listener lease until its own socket pathname has been removed."""

    def __init__(
        self,
        server: socket.socket,
        path: Path,
        lease: int,
        identity: tuple[int, int],
    ) -> None:
        """Retain the bound socket, locked file descriptor, and pathname identity."""
        self.socket = server
        self.path = path
        self.lease = lease
        self.identity = identity
        self.closed = False
        self.pending: asyncio.Task | None = None

    async def accept(self) -> Connection:
        """Accept and authorize one peer while keeping cancellation local to this wait."""
        if self.closed:
            raise IpcClosedError("IPC listener is closed.")
        task = asyncio.create_task(
            asyncio.get_running_loop().sock_accept(self.socket)
        )
        self.pending = task
        peer = None
        try:
            peer, _ = await task
            peer.setblocking(False)
            return Connection(peer, verify_peer(peer))
        except asyncio.CancelledError:
            if (
                peer is None
                and task.done()
                and not task.cancelled()
                and task.exception() is None
            ):
                peer, _ = task.result()
            if peer is not None:
                peer.close()
            if self.closed:
                raise IpcClosedError("IPC listener is closed.") from None
            raise
        except OSError as error:
            if peer is not None:
                peer.close()
            raise IpcClosedError("IPC socket accept failed.") from error
        except BaseException:
            if peer is not None:
                peer.close()
            raise
        finally:
            self.pending = None

    async def close(self) -> None:
        """Cancel accept and unlink only the socket owned by this listener."""
        if self.closed:
            return
        self.closed = True
        if self.pending is not None:
            self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
        self.socket.close()
        try:
            remove_socket(self.path, self.identity)
        finally:
            os.close(self.lease)


def remove_socket(path: Path, identity: tuple[int, int]) -> None:
    """Remove a socket only if it still has the listener's device and inode."""
    try:
        info = path.lstat()
        if (info.st_dev, info.st_ino) == identity and stat.S_ISSOCK(info.st_mode):
            path.unlink()
    except FileNotFoundError:
        pass


async def connect(name: str) -> Connection:
    """Connect to a private socket and authorize its kernel-reported owner."""
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.setblocking(False)
    try:
        await asyncio.get_running_loop().sock_connect(peer, str(endpoint_path(name)))
        return Connection(peer, verify_peer(peer))
    except OSError as error:
        peer.close()
        if error.errno in (errno.ENOENT, errno.ECONNREFUSED, errno.EAGAIN):
            raise IpcUnavailableError("IPC endpoint is unavailable.") from error
        raise IpcError("Cannot connect to the private IPC endpoint.") from error
    except BaseException:
        peer.close()
        raise


async def listen(name: str) -> Listener:
    """Acquire a native lease before checking or replacing an abandoned socket."""
    path = endpoint_path(name)
    lease = None
    server = None
    identity = None
    try:
        lease = os.open(
            path.with_suffix(".lock"),
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        info = os.fstat(lease)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            raise IpcError("IPC ownership file must be private and owned by the current user.")
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IpcAddressInUseError("IPC endpoint already has a listener.") from error
        try:
            previous = path.lstat()
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.geteuid():
                raise IpcError("Refusing to replace an unrecognized IPC endpoint.")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.setblocking(False)
            try:
                await asyncio.wait_for(
                    asyncio.get_running_loop().sock_connect(probe, str(path)),
                    timeout=0.2,
                )
            except OSError as error:
                if error.errno not in (errno.ENOENT, errno.ECONNREFUSED):
                    raise IpcError("Cannot verify the existing IPC endpoint.") from error
            except TimeoutError as error:
                raise IpcAddressInUseError("IPC endpoint may still have a listener.") from error
            else:
                raise IpcAddressInUseError("IPC endpoint already has a listener.")
            finally:
                probe.close()
            remove_socket(path, (previous.st_dev, previous.st_ino))
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.setblocking(False)
        server.bind(str(path))
        info = path.lstat()
        identity = (info.st_dev, info.st_ino)
        path.chmod(0o600)
        server.listen(128)
        return Listener(server, path, lease, identity)
    except BaseException as error:
        if server is not None:
            server.close()
        if identity is not None:
            remove_socket(path, identity)
        if lease is not None:
            os.close(lease)
        if isinstance(error, OSError):
            raise IpcError("Cannot claim the private IPC endpoint.") from error
        raise
