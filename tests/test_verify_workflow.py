"""Regression coverage for the small, browser-free Apps verification wrapper."""

from __future__ import annotations

from pathlib import Path


_ROOT = Path(__file__).parents[1]


def _function_body(script: str, name: str) -> str:
    start = script.index(f"function {name}")
    end = script.find("\nfunction ", start + 1)
    if end == -1:
        end = script.index("\nswitch ", start + 1)
    return script[start:end]


def test_apps_mode_runs_only_the_browser_free_apps_workflow() -> None:
    script = (_ROOT / "scripts" / "verify.ps1").read_text(encoding="utf-8")
    apps = _function_body(script, "Invoke-Apps")

    assert '& npm ci --prefix frontend' in apps
    assert '& npm run build --prefix frontend' in apps
    assert '& npm test --prefix frontend' in apps
    assert 'MCP App packaging and protocol tests' in apps
    assert "tests/test_mcp_apps.py" in apps
    assert "tests/test_verify_workflow.py" in apps
    for forbidden in ("puppeteer", "chrome", "chromium", "browser-harness", "playwright", "selenium", "jsdom", "inspector"):
        assert forbidden not in apps.lower()


def test_verify_wrapper_remains_windows_powershell_compatible() -> None:
    script = (_ROOT / "scripts" / "verify.ps1").read_text(encoding="utf-8")

    assert "#requires -Version 5.1" in script
    assert "Set-StrictMode -Version Latest" in script
    assert "Invoke-CheckedCommand" in script


def test_bootstrap_has_a_non_mutating_prerequisite_validation_mode() -> None:
    script = (_ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")

    assert "#requires -Version 5.1" in script
    assert "[switch]$ValidateOnly" in script
    assert "Bootstrap prerequisites validated; no files were changed." in script


def test_bootstrap_installs_only_frontend_dependencies() -> None:
    script = (_ROOT / "scripts" / "bootstrap.ps1").read_text(encoding="utf-8")

    assert "npm ci --prefix frontend" in script
    for forbidden in ("puppeteer", "chrome", "chromium", "browser-dependency", "browsers install"):
        assert forbidden not in script.lower()
