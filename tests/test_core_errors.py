from forgemcp.core.errors import ServiceNotFoundError, to_mcp_error_response


def test_expected_error_has_safe_structured_mcp_response():
    response = to_mcp_error_response(ServiceNotFoundError("Service is not registered: cmake"))

    assert response.as_dict() == {
        "ok": False,
        "error": {
            "code": "service_not_found",
            "message": "Service is not registered: cmake",
        },
    }
