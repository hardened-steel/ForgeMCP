"""Small dependency container shared by Core and future modules."""

from __future__ import annotations

from collections.abc import Iterator

from forgemcp.core.errors import ServiceAlreadyRegisteredError, ServiceNotFoundError


class ServiceRegistry:
    """Named service container with explicit registration and lookup."""

    def __init__(self) -> None:
        self._services: dict[str, object] = {}

    def register(self, name: str, service: object) -> None:
        """Register one service under a stable non-empty name."""
        if not name:
            raise ValueError("Service name must not be empty.")
        if name in self._services:
            raise ServiceAlreadyRegisteredError(f"Service already registered: {name}")
        self._services[name] = service

    def get(self, name: str) -> object:
        """Return a registered service or raise a domain-specific error."""
        try:
            return self._services[name]
        except KeyError as error:
            raise ServiceNotFoundError(f"Service is not registered: {name}") from error

    def names(self) -> tuple[str, ...]:
        """Return registered service names in stable order."""
        return tuple(sorted(self._services))

    def __contains__(self, name: object) -> bool:
        return name in self._services

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())
