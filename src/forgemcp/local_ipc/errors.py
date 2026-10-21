"""Expected failures of private, framed local IPC connections."""


class IpcError(Exception):
    """A local IPC endpoint could not complete the requested operation."""


class IpcUnavailableError(IpcError):
    """The requested endpoint is absent or temporarily cannot accept a connection."""


class IpcAddressInUseError(IpcError):
    """Another listener owns the requested endpoint."""


class IpcClosedError(IpcError):
    """The connection or listener has closed."""


class IpcProtocolError(IpcError):
    """A frame is invalid or the peer disconnected inside a frame."""
