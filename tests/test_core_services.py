import pytest

from forgemcp.core.errors import ServiceAlreadyRegisteredError, ServiceNotFoundError
from forgemcp.core.services import ServiceRegistry


def test_registry_registers_and_lists_services():
    registry = ServiceRegistry()
    service = object()

    registry.register("service", service)

    assert registry.get("service") is service
    assert registry.names() == ("service",)


def test_registry_rejects_duplicate_and_missing_service():
    registry = ServiceRegistry()
    registry.register("service", object())

    with pytest.raises(ServiceAlreadyRegisteredError):
        registry.register("service", object())
    with pytest.raises(ServiceNotFoundError):
        registry.get("missing")
