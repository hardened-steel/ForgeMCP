"""Regression coverage for the bounded, offline Apps verification wrapper."""

from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).parents[1]


def _function_body(script: str, name: str) -> str:
    start = script.index(f"function {name}")
    end = script.index("\nfunction ", start + 1)
    return script[start:end]


def test_apps_mode_uses_direct_node_entrypoints_and_no_command_wrappers() -> None:
    script = (_ROOT / "scripts" / "verify.ps1").read_text(encoding="utf-8")
    apps = _function_body(script, "Invoke-Apps")

    assert 'Invoke-CheckedProcess "frontend build" "node.exe" @("frontend/git-status/build.mjs") 60' in apps
    assert 'Invoke-CheckedProcess "frontend unit and production browser harness" "node.exe" @("--test", "frontend/git-status/test.mjs") 120' in apps
    assert 'Invoke-CheckedProcess "MCP App packaging/protocol tests" $python' in apps
    assert "tests/test_mcp_apps.py" in apps
    for forbidden in ("npm", ".cmd", "powershell.exe", "cmd.exe", "npx", "invoke-expression", "invoke-webrequest", "curl", "wget"):
        assert forbidden not in apps.lower()


def test_verify_wrapper_has_a_windows_powershell_compatible_argv_round_trip_gate() -> None:
    script = (_ROOT / "scripts" / "verify.ps1").read_text(encoding="utf-8")
    probe = (_ROOT / "tests" / "fixtures" / "verify_argv_probe.py").read_text(encoding="utf-8")

    assert "#requires -Version 5.1" in script
    assert "function ConvertTo-WindowsCommandLineArgument" in script
    assert "function Set-ProcessStartInfoArguments" in script
    assert "PSObject.Properties['ArgumentList']" in script
    assert "Process arguments cannot contain NUL characters." in script
    assert "argv round-trip probe" in script
    for value in ("with space", "C:\\Program Files\\LLVM\\bin", "--value=a b", "кириллица"):
        assert value in script
    assert "json.dumps(sys.argv[1:], ensure_ascii=False)" in probe


def test_bootstrap_has_a_non_mutating_prerequisite_validation_mode() -> None:
    script = (_ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")

    assert "#requires -Version 5.1" in script
    assert "[switch]$ValidateOnly" in script
    assert "Bootstrap prerequisites validated; no files were changed." in script


def test_chromium_harness_uses_headless_software_compositing_without_weakening_security() -> None:
    harness = (_ROOT / "frontend" / "git-status" / "browser-harness.mjs").read_text(encoding="utf-8")

    assert '"--headless=new"' in harness
    assert '"--disable-gpu"' in harness
    assert '"--no-sandbox"' not in harness
    assert '"--disable-setuid-sandbox"' not in harness
    assert "--user-data-dir=${profile}" in harness
    assert 'readFile(assetPath)' in harness
    assert "chromium_stderr_category" in harness
    assert "Chromium stderr:" not in harness
