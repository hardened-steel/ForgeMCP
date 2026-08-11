from forgemcp.core.logging import sanitize_log_context


def test_log_context_redacts_content_and_secrets_recursively():
    result = sanitize_log_context(
        {
            "file_content": "int main() {}",
            "token": "abc",
            "operation": "server_started",
            "nested": {"password": "hidden", "target": "app"},
        }
    )

    assert result == {
        "file_content": "<redacted>",
        "token": "<redacted>",
        "operation": "server_started",
        "nested": {"password": "<redacted>", "target": "app"},
    }
