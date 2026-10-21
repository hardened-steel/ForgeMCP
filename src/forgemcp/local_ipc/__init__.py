"""Private same-user local IPC with a platform-independent byte-frame API."""

from .errors import (
    IpcAddressInUseError,
    IpcClosedError,
    IpcError,
    IpcProtocolError,
    IpcUnavailableError,
)
from .service import IpcConnection, IpcListener, connect, listen, validate_name

__all__ = [
    "IpcAddressInUseError",
    "IpcClosedError",
    "IpcConnection",
    "IpcError",
    "IpcListener",
    "IpcProtocolError",
    "IpcUnavailableError",
    "connect",
    "listen",
    "validate_name",
]
