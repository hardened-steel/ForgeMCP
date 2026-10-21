"""Expected failures at the local admin HTTP and IPC boundary."""


class HubRequestError(Exception):
    """An instance cannot answer a request or returned an expected protocol error."""

    def __init__(self, message: str, status: int = 503) -> None:
        """Retain a safe public message and its HTTP status."""
        super().__init__(message)
        self.status = status
