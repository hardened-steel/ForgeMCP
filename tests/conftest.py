"""One-switch, host-qualified ForgeMCP live acceptance orchestration.

The option is intentionally a test harness concern: it invokes the same
application-scoped discovery service as production, but never changes server
configuration precedence or installs test-only executable paths.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from forgemcp.core.config import ForgeConfig
from forgemcp.toolchain import ToolchainDiscoveryService
from tests.acceptance_manifest import TOOL_ACCEPTANCE, coverage_records, reset_coverage_records, write_coverage_report


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-forgemcp-live-acceptance",
        action="store_true",
        default=False,
        help="run the unified production-discovery real-MCP C++ acceptance tier",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "msvc_live_mcp: MSVC real-MCP gate controlled by the unified runner")
    if not config.getoption("--run-forgemcp-live-acceptance"):
        return
    discovery = ToolchainDiscoveryService(ForgeConfig(workspace_root=Path.cwd()))
    snapshot = discovery.snapshot().as_dict()
    kits = discovery.kits().model_dump(mode="json").get("kits", [])
    tools = {str(item["tool"]): bool(item["available"]) for item in snapshot["tools"]}  # type: ignore[index]
    ready = [item for item in kits if item.get("readiness") == "ready"]
    capabilities = {
        "cmake_ctest_ninja": all(tools.get(name, False) for name in ("cmake", "ctest", "ninja")),
        "msvc": any(item.get("compiler_family") == "msvc" for item in ready),
        "standalone_clang": any(
            item.get("origin") == "standalone" and item.get("compiler_family") == "clang"
            for item in ready
        ),
        "clangd": tools.get("clangd", False),
        "clang_format": tools.get("clang-format", False),
        "clang_tidy": tools.get("clang-tidy", False),
        "qualified_lldb_dap": bool(tools.get("lldb-dap", False)) and any(
            item.get("origin") == "standalone" and item.get("compiler_family") == "clang"
            and item.get("debugger_compatibility") == "compatible" for item in ready
        ),
    }
    # Only booleans and public metadata enter the report.  Paths, PATH and
    # discovery rejection details deliberately remain absent.
    config._forgemcp_live_capabilities = capabilities  # type: ignore[attr-defined]
    config._forgemcp_live_discovery = {  # type: ignore[attr-defined]
        "schema_version": 1,
        "capabilities": capabilities,
        "tool_sources": {
            str(item["tool"]): str(item.get("source", "discovery"))
            for item in snapshot["tools"]  # type: ignore[index]
        },
        "ready_kit_families": sorted({str(item.get("compiler_family")) for item in ready}),
    }
    reset_coverage_records()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-forgemcp-live-acceptance"):
        for item in items:
            if item.get_closest_marker("msvc_live_mcp"):
                item.add_marker(pytest.mark.skip(reason="MSVC live gate is run only by --run-forgemcp-live-acceptance"))
        return
    capabilities: dict[str, bool] = config._forgemcp_live_capabilities  # type: ignore[attr-defined]
    for item in items:
        if item.get_closest_marker("msvc_live_mcp") and not capabilities["msvc"]:
            item.add_marker(pytest.mark.skip(reason="capability_absent: production discovery found no ready MSVC kit"))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    if not config.getoption("--run-forgemcp-live-acceptance"):
        return
    root = Path(tempfile.gettempdir()) / "forgemcp-live-acceptance"
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "coverage.json"
    discovery_path = root / "capabilities.json"
    discovery_path.write_text(json.dumps(config._forgemcp_live_discovery, sort_keys=True) + "\n", encoding="utf-8")  # type: ignore[attr-defined]
    try:
        records = coverage_records()
        expected = {(entry.tool_name, entry.scenario_id) for entry in TOOL_ACCEPTANCE}
        actual = {(record.tool_name, record.scenario_id) for record in records}
        if len(actual) != len(records) or actual - expected:
            raise AssertionError("coverage has duplicate or orphan SDK records")
        # A normal success path must have been asserted whenever its declared
        # host capabilities are available.  A skipped live scenario is valid
        # only if the production discovery report proves its capability absent.
        available = config._forgemcp_live_capabilities  # type: ignore[attr-defined]
        aliases = {"mcp_stdio": True, "cmake": available["cmake_ctest_ninja"], "ninja": available["cmake_ctest_ninja"], "clangd": available["clangd"], "compile_commands": available["standalone_clang"], "standalone_llvm": available["standalone_clang"], "lldb_dap": available["qualified_lldb_dap"], "dwarf_debuggee": available["qualified_lldb_dap"]}
        missing = []
        for entry in TOOL_ACCEPTANCE:
            required = all(aliases.get(capability, False) for capability in entry.required_host_capabilities)
            record = next((item for item in records if item.tool_name == entry.tool_name and item.scenario_id == entry.scenario_id), None)
            if required and (record is None or record.calls <= 0 or not record.meaningful_assertion_completed):
                missing.append(entry.tool_name)
        if missing:
            raise AssertionError("available live capability was skipped or lacked a successful SDK call: " + ", ".join(missing))
        write_coverage_report(report_path)
        session.config._forgemcp_live_report_paths = (discovery_path, report_path)  # type: ignore[attr-defined]
    except AssertionError as error:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line(f"ForgeMCP unified live acceptance FAILED: {error}")
    else:
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line(f"ForgeMCP live capability report: {discovery_path}")
            terminal.write_line(f"ForgeMCP live SDK coverage report: {report_path}")
