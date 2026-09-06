"""Expected process execution failures."""


class ProcessError(Exception):
    """Base class for expected process failures."""


class ProcessStartError(ProcessError):
    """Raised when an external process cannot be started."""


class ProcessNotFoundError(ProcessError):
    """Raised when an unknown tracked process is requested."""


class ProcessExitedError(ProcessError):
    """Raised when process I/O is attempted after exit."""


class ProcessStreamError(ProcessError):
    """Raised when a process stream is consumed through an incompatible API."""
