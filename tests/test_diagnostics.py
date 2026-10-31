"""Shared diagnostic limits, catalog hints, and native-path redaction."""

import errno

from forgemcp.diagnostics import available_values, display_value, os_error_reason


def test_identifier_values_escape_controls_and_bound_untrusted_text():
    """Keep arbitrary identifiers on one bounded line without printing their complete payload."""
    value = "\n\0\x1b" * 1000 + "PRIVATE_SUFFIX"
    rendered = display_value(value)
    assert "\n" not in rendered and "\0" not in rendered and "\x1b" not in rendered
    assert "\\n" in rendered and "\\x00" in rendered and "\\x1b" in rendered
    assert "PRIVATE_SUFFIX" not in rendered and rendered.endswith("...")
    assert len(rendered) < 500


def test_catalog_hints_explain_empty_small_and_large_sets():
    """List every small choice, bound large catalogs, and preserve a real discovery call."""
    assert "No values are currently available" in available_values([], "toolsets_list()")
    small = available_values(["release", "debug", "debug"], "cmake_profiles()")
    assert "'debug', 'release'" in small and "cmake_profiles()" in small
    assert "'7'" in available_values(map(str, range(8)), "processes_overview()")
    large = available_values(map(str, range(9)), "processes_overview()")
    assert "9 available values" in large and "'8'" not in large
    assert "processes_overview()" in large


def test_os_error_keeps_localized_reason_without_native_filenames():
    """Use the system reason without leaking either source or destination from an OSError."""
    error = OSError(errno.EACCES, "Доступ запрещён", "C:/private/source", 5, "D:/private/target")
    assert os_error_reason(error) == "Доступ запрещён"
    assert os_error_reason(PermissionError()) == "PermissionError"
