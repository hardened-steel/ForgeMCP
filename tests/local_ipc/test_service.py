"""Private local IPC framing, ownership, authorization, and cancellation tests."""

import asyncio
import os
import struct
from uuid import uuid4

import pytest

from forgemcp.local_ipc import (
    IpcAddressInUseError,
    IpcClosedError,
    IpcError,
    IpcProtocolError,
    IpcUnavailableError,
    connect,
    listen,
)


@pytest.fixture
def anyio_backend():
    """Use the asyncio loop required by the native local IPC backends."""
    return "asyncio"


@pytest.fixture
def endpoint_name():
    """Give each test an endpoint unrelated to a running ForgeMCP instance."""
    return f"test-{uuid4().hex}"


@pytest.fixture(autouse=True)
def unix_runtime(request, monkeypatch):
    """Keep Unix runtime files inside the test's temporary directory."""
    if os.name != "nt":
        from forgemcp.local_ipc import unix

        tmp_path = request.getfixturevalue("tmp_path_factory").mktemp("ipc")
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        monkeypatch.setattr(unix.tempfile, "gettempdir", lambda: str(tmp_path))


@pytest.fixture
async def pair(endpoint_name):
    """Yield two authorized peers and clean up every native resource on failure."""
    async with asyncio.timeout(15):
        listener = await listen(endpoint_name)
        client = None
        server = None
        try:
            client = await connect(endpoint_name)
            server = await listener.accept()
            yield client, server
        finally:
            if client is not None:
                await client.close()
            if server is not None:
                await server.close()
            await listener.close()


@pytest.mark.anyio
async def test_duplex_binary_and_empty_frames(pair):
    """Preserve binary payloads and distinguish an empty frame from clean EOF."""
    client, server = pair
    payloads = [b"", b"\x00\xff\r\n", "α🙂".encode(), bytes(range(256)) * 512]
    for payload in payloads:
        sender = asyncio.create_task(client.send(payload))
        assert await server.receive() == payload
        await sender
        sender = asyncio.create_task(server.send(payload))
        assert await client.receive() == payload
        await sender
    await client.close()
    assert await server.receive() is None


@pytest.mark.anyio
async def test_kernel_peer_identity(pair):
    """Use a kernel peer PID where available rather than peer-supplied data."""
    client, server = pair
    assert client.peer_pid in (None, os.getpid())
    assert server.peer_pid in (None, os.getpid())
    if os.name == "nt":
        assert client.peer_pid == server.peer_pid == os.getpid()


@pytest.mark.anyio
async def test_simultaneous_senders_preserve_frames(pair):
    """Serialize complete frames while a receiver drains native backpressure."""
    client, server = pair
    payloads = [str(index).encode() + b":" + bytes([index]) * 100000 for index in range(12)]

    async def collect():
        """Read every frame without assuming scheduling order between senders."""
        return [await server.receive() for _ in payloads]

    receiver = asyncio.create_task(collect())
    await asyncio.gather(*(client.send(payload) for payload in payloads))
    assert sorted(await receiver) == sorted(payloads)


@pytest.mark.anyio
async def test_read_and_write_are_independent(pair):
    """Drain large transfers concurrently in both directions of the same pipe."""
    client, server = pair
    left = b"left" * 200000
    right = b"right" * 200000
    results = await asyncio.gather(
        client.send(left),
        server.send(right),
        client.receive(),
        server.receive(),
    )
    assert results[2:] == [right, left]


@pytest.mark.anyio
@pytest.mark.parametrize("raw", [b"\x00", b"\x00\x00\x00\x05ab"])
async def test_partial_frame_eof_is_protocol_failure(pair, raw):
    """Reject a truncated prefix or payload instead of reporting clean EOF."""
    client, server = pair
    await client.transport.write(raw)
    await client.close()
    with pytest.raises(IpcProtocolError, match="inside an IPC frame"):
        await server.receive()
    assert server.closed


@pytest.mark.anyio
async def test_fragmented_native_reads_preserve_frame(pair):
    """Combine a fragmented prefix and payload without depending on write boundaries."""
    client, server = pair
    receiver = asyncio.create_task(server.receive())
    for part in (b"\x00", b"\x00\x00", b"\x05", b"ab", b"cde"):
        await client.transport.write(part)
        await asyncio.sleep(0)
    assert await receiver == b"abcde"


@pytest.mark.anyio
async def test_unavailable_endpoint(endpoint_name):
    """Report an absent endpoint without creating a listener implicitly."""
    with pytest.raises(IpcUnavailableError):
        await connect(endpoint_name)


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["", "../outside", "remote\\pipe", "space name", "α", "a" * 97])
async def test_reject_nonlogical_endpoint_names(name):
    """Reject paths and unbounded names before reaching platform APIs."""
    for operation in (connect, listen):
        with pytest.raises(IpcError, match="IPC name"):
            await operation(name)


@pytest.mark.anyio
async def test_listener_ownership_survives_empty_connections(endpoint_name):
    """Retain exclusive ownership before and after the last data connection closes."""
    listener = await listen(endpoint_name)
    try:
        for _ in range(2):
            with pytest.raises(IpcAddressInUseError):
                await listen(endpoint_name)
            client = await connect(endpoint_name)
            server = await listener.accept()
            await client.close()
            await server.close()
        with pytest.raises(IpcAddressInUseError):
            await listen(endpoint_name)
    finally:
        await listener.close()
    replacement = await listen(endpoint_name)
    await replacement.close()


@pytest.mark.anyio
async def test_concurrent_listener_candidates_have_one_winner(endpoint_name):
    """Elect exactly one listener even when several candidates start concurrently."""
    results = await asyncio.gather(
        *(listen(endpoint_name) for _ in range(5)),
        return_exceptions=True,
    )
    winners = [result for result in results if not isinstance(result, BaseException)]
    try:
        assert len(winners) == 1
        assert all(
            isinstance(result, IpcAddressInUseError) or result in winners
            for result in results
        )
    finally:
        for winner in winners:
            await winner.close()


@pytest.mark.anyio
async def test_cancel_accept_keeps_listener_usable(endpoint_name):
    """Cancel a pending native accept without releasing listener ownership."""
    listener = await listen(endpoint_name)
    try:
        pending = asyncio.create_task(listener.accept())
        await asyncio.sleep(0.03)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        client = await connect(endpoint_name)
        server = await listener.accept()
        await client.send(b"after cancellation")
        assert await server.receive() == b"after cancellation"
        await client.close()
        await server.close()
    finally:
        await listener.close()


@pytest.mark.anyio
async def test_close_listener_wakes_pending_accept(endpoint_name):
    """Complete a pending accept with a domain error when its listener closes."""
    listener = await listen(endpoint_name)
    pending = asyncio.create_task(listener.accept())
    await asyncio.sleep(0.03)
    await listener.close()
    with pytest.raises(IpcClosedError):
        await pending
    await listener.close()
    with pytest.raises(IpcClosedError):
        await listener.accept()


@pytest.mark.anyio
async def test_cancel_receive_closes_desynchronized_connection(pair):
    """Cancel an incomplete frame and close both its pending I/O and future reads."""
    client, server = pair
    receiver = asyncio.create_task(server.receive())
    await client.transport.write(struct.pack("!I", 50) + b"partial")
    await asyncio.sleep(0.03)
    receiver.cancel()
    with pytest.raises(asyncio.CancelledError):
        await receiver
    assert server.closed
    assert await client.receive() is None
    with pytest.raises(IpcClosedError):
        await server.receive()


@pytest.mark.anyio
async def test_close_connection_wakes_pending_receive(pair):
    """Allow a separate close task to terminate a pending native read safely."""
    _, server = pair
    receiver = asyncio.create_task(server.receive())
    await asyncio.sleep(0.03)
    await server.close()
    with pytest.raises(IpcClosedError):
        await receiver
    await server.close()


@pytest.mark.anyio
async def test_cancel_backpressured_send_closes_connection(pair):
    """Cancel a blocked native write without freeing its buffer before completion."""
    client, _ = pair
    sender = asyncio.create_task(client.send(b"x" * 8000000))
    await asyncio.sleep(0.03)
    assert not sender.done()
    sender.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sender
    assert client.closed
    with pytest.raises(IpcClosedError):
        await client.send(b"next")


@pytest.mark.anyio
async def test_invalid_payload_does_not_damage_connection(pair):
    """Reject an invalid outgoing type before emitting any frame bytes."""
    client, server = pair
    with pytest.raises(TypeError, match="must be bytes"):
        await client.send("text")
    await client.send(b"still usable")
    assert await server.receive() == b"still usable"


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows pipe security descriptor")
async def test_windows_pipe_has_explicit_current_user_acl(pair):
    """Verify the pipe DACL has one explicit ACE for the current process user."""
    import win32security
    from forgemcp.local_ipc import windows

    _, server = pair
    descriptor = win32security.GetSecurityInfo(
        server.transport.handle,
        win32security.SE_KERNEL_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    acl = descriptor.GetSecurityDescriptorDacl()
    assert acl.GetAceCount() == 1
    assert acl.GetAce(0)[2] == windows.current_sid()


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows pipe peer token")
async def test_windows_rejects_other_peer_user(pair, monkeypatch):
    """Reject a kernel peer whose primary token differs from the required user."""
    import win32security
    from forgemcp.local_ipc import windows

    _, server = pair
    everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid)
    monkeypatch.setattr(windows, "current_sid", lambda: everyone)
    with pytest.raises(IpcError, match="another user"):
        windows.verify_peer(server.transport.handle, server=False)


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows named pipe creation flags")
async def test_windows_claim_and_data_pipes_reject_remote_clients(endpoint_name, monkeypatch):
    """Require remote rejection and exclusive ownership without inherited handles."""
    from forgemcp.local_ipc import windows

    original = windows.win32pipe.CreateNamedPipe
    calls = []

    def create_pipe(*arguments):
        """Observe real pipe creation without replacing the native implementation."""
        calls.append(arguments)
        return original(*arguments)

    monkeypatch.setattr(windows.win32pipe, "CreateNamedPipe", create_pipe)
    listener = await listen(endpoint_name)
    await listener.close()
    assert len(calls) == 2
    assert calls[0][1] & windows.Connection.FIRST_PIPE_INSTANCE
    assert all(arguments[2] & windows.Connection.REJECT_REMOTE_CLIENTS for arguments in calls)
    assert all(not arguments[-1].bInheritHandle for arguments in calls)


@pytest.mark.anyio
@pytest.mark.skipif(os.name == "nt", reason="Unix filesystem socket ownership")
async def test_unix_reclaims_only_abandoned_socket(endpoint_name):
    """Recover a stale socket under the listener lease without accepting live owners."""
    import socket
    from forgemcp.local_ipc import unix

    path = unix.endpoint_path(endpoint_name)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()
    listener = await listen(endpoint_name)
    try:
        assert path.stat().st_mode & 0o077 == 0
        assert path.parent.stat().st_mode & 0o077 == 0
    finally:
        await listener.close()
    assert not path.exists()


@pytest.mark.anyio
@pytest.mark.skipif(os.name == "nt", reason="Unix filesystem socket ownership")
async def test_unix_does_not_unlink_live_foreign_listener(endpoint_name):
    """Preserve an active socket even if its owner does not use our lock file."""
    import socket
    from forgemcp.local_ipc import unix

    path = unix.endpoint_path(endpoint_name)
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(path))
    live.listen(1)
    try:
        with pytest.raises(IpcAddressInUseError):
            await listen(endpoint_name)
        assert path.exists()
    finally:
        live.close()
        path.unlink()


@pytest.mark.anyio
@pytest.mark.skipif(os.name == "nt", reason="Unix filesystem socket ownership")
async def test_unix_close_preserves_replaced_path(endpoint_name):
    """Avoid deleting a path that replaced the listener's socket inode."""
    from forgemcp.local_ipc import unix

    listener = await listen(endpoint_name)
    path = unix.endpoint_path(endpoint_name)
    path.unlink()
    path.write_text("replacement", encoding="utf-8")
    await listener.close()
    assert path.read_text(encoding="utf-8") == "replacement"
