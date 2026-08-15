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


def test_log_context_redacts_mixed_case_secrets_paths_and_exceptions():
    result = sanitize_log_context({
        "Api_SeCrEt": "value",
        "detail": r"C:\\Users\\operator\\secret.txt",
        "exception": RuntimeError(r"C:\\hidden\\token"),
    })

    assert result == {
        "Api_SeCrEt": "<redacted>",
        "detail": "<redacted>",
        "exception": "<redacted>",
    }
