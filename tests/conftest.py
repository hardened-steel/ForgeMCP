"""One-switch, host-qualified ForgeMCP live acceptance orchestration.

The option is intentionally a test harness concern: it invokes the same
application-scoped discovery service as production, but never changes server
configuration precedence or installs test-only executable paths.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime
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
    report_root = Path(tempfile.gettempdir()) / "forgemcp-live-acceptance"
    report_root.mkdir(parents=True, exist_ok=True)
    # A failed, interrupted, or capability-incomplete run must not leave an
    # earlier successful artifact looking current.
    for name in ("coverage.json", "coverage.json.tmp", "capabilities.json"):
        (report_root / name).unlink(missing_ok=True)
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
        "visual_studio_clang": any(
            item.get("origin") == "visual_studio" and item.get("compiler_family") == "clang"
            for item in ready
        ),
        "clang_cl": any(item.get("compiler_family") == "clang-cl" for item in ready),
        "clangd": tools.get("clangd", False),
        "clang_format": tools.get("clang-format", False),
        "clang_tidy": tools.get("clang-tidy", False),
        "qualified_lldb_dap": bool(tools.get("lldb-dap", False)) and any(
            item.get("origin") == "standalone" and item.get("compiler_family") == "clang"
            and item.get("debugger_compatibility") == "compatible" for item in ready
        ),
        "git": tools.get("git", False),
    }
    # Only booleans and public metadata enter the report.  Paths, PATH and
    # discovery rejection details deliberately remain absent.
    config._forgemcp_live_capabilities = capabilities  # type: ignore[attr-defined]
    config._forgemcp_live_discovery = {  # type: ignore[attr-defined]
        "schema_version": 1,
        "started_at_utc": datetime.now(UTC).isoformat(),
        "discovery_candidates": capabilities,
        "capabilities": {name: False for name in capabilities},
        "qualification": {
            name: "candidate_detected_unqualified" if present else "candidate_absent"
            for name, present in capabilities.items()
        },
        "tool_sources": {
            str(item["tool"]): str(item.get("source", "discovery"))
            for item in snapshot["tools"]  # type: ignore[index]
        },
        "ready_kit_families": sorted({str(item.get("compiler_family")) for item in ready}),
        "ready_kit_profiles": [
            {
                "origin": str(item.get("origin", "unknown")),
                "compiler_family": str(item.get("compiler_family", "unknown")),
                "driver_mode": str(item.get("driver_mode", "unknown")),
                "abi": str(item.get("abi", "unknown")),
                "debugger_compatibility": str(item.get("debugger_compatibility", "unavailable")),
            }
            for item in ready[:32]
        ],
        "executed_nodes": [],
    }
    config._forgemcp_live_node_groups = {}  # type: ignore[attr-defined]
    config._forgemcp_live_node_outcomes = {}  # type: ignore[attr-defined]
    reset_coverage_records()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if not config.getoption("--run-forgemcp-live-acceptance"):
        for item in items:
            if item.get_closest_marker("msvc_live_mcp"):
                item.add_marker(pytest.mark.skip(reason="MSVC live gate is run only by --run-forgemcp-live-acceptance"))
        return
    capabilities: dict[str, bool] = config._forgemcp_live_capabilities  # type: ignore[attr-defined]
    for item in items:
        group: str | None = None
        if item.get_closest_marker("msvc_live_mcp"):
            group = "msvc"
        elif item.get_closest_marker("clangd_fixture_mcp"):
            group = "clangd"
        elif item.get_closest_marker("debugger_fixture_mcp"):
            group = "debugger"
        elif item.get_closest_marker("git_fixture_mcp"):
            group = "git"
        elif item.name == "test_cpp_acceptance_fixture_mcp_surface_and_real_cmake_workspace_quality_flow":
            group = "cmake_quality"
        if group is not None:
            config._forgemcp_live_node_groups[item.nodeid] = group  # type: ignore[attr-defined]
        if item.get_closest_marker("msvc_live_mcp") and not capabilities["msvc"]:
            item.add_marker(pytest.mark.skip(reason="capability_absent: production discovery found no ready MSVC kit"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]):
    outcome = yield
    report = outcome.get_result()
    config = item.config
    if not config.getoption("--run-forgemcp-live-acceptance"):
        return
    groups: dict[str, str] = config._forgemcp_live_node_groups  # type: ignore[attr-defined]
    if item.nodeid not in groups:
        return
    if report.skipped:
        status = "skipped"
    elif report.failed:
        status = "failed"
    elif report.when == "call" and report.passed:
        status = "passed"
    else:
        return
    config._forgemcp_live_node_outcomes[item.nodeid] = status  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    config = session.config
    if not config.getoption("--run-forgemcp-live-acceptance"):
        return
    root = Path(tempfile.gettempdir()) / "forgemcp-live-acceptance"
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "coverage.json"
    discovery_path = root / "capabilities.json"
    discovery_report: dict[str, object] = config._forgemcp_live_discovery  # type: ignore[attr-defined]
    candidates: dict[str, bool] = config._forgemcp_live_capabilities  # type: ignore[attr-defined]
    groups: dict[str, str] = config._forgemcp_live_node_groups  # type: ignore[attr-defined]
    outcomes: dict[str, str] = config._forgemcp_live_node_outcomes  # type: ignore[attr-defined]
    by_group: dict[str, list[str]] = {}
    executed_nodes: list[dict[str, str]] = []
    for nodeid, group in sorted(groups.items()):
        status = outcomes.get(nodeid, "not_run")
        by_group.setdefault(group, []).append(status)
        executed_nodes.append({
            "node": nodeid.rsplit("::", 1)[-1][:256],
            "group": group,
            "outcome": status,
        })
    qualification_nodes = {
        "cmake_ctest_ninja": "cmake_quality",
        "clang_format": "cmake_quality",
        "clang_tidy": "cmake_quality",
        "standalone_clang": "clangd",
        "clangd": "clangd",
        "qualified_lldb_dap": "debugger",
        "git": "git",
        "msvc": "msvc",
    }
    qualification: dict[str, str] = {}
    qualified: dict[str, bool] = {}
    for name, candidate in candidates.items():
        statuses = by_group.get(qualification_nodes.get(name, ""), [])
        is_qualified = bool(candidate and statuses and all(status == "passed" for status in statuses))
        qualified[name] = is_qualified
        qualification[name] = (
            "qualified" if is_qualified
            else "candidate_detected_unqualified" if candidate
            else "candidate_absent"
        )
    discovery_report["capabilities"] = qualified
    discovery_report["qualification"] = qualification
    discovery_report["executed_nodes"] = executed_nodes[:32]
    discovery_encoded = json.dumps(discovery_report, sort_keys=True, separators=(",", ":")) + "\n"
    if len(discovery_encoded.encode("utf-8")) > 32 * 1024:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        return
    discovery_path.write_text(discovery_encoded, encoding="utf-8")
    if exitstatus != pytest.ExitCode.OK:
        report_path.unlink(missing_ok=True)
        return
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
        aliases = {"mcp_stdio": True, "cmake": available["cmake_ctest_ninja"], "ninja": available["cmake_ctest_ninja"], "git": available["git"], "clangd": available["clangd"], "compile_commands": available["standalone_clang"], "standalone_llvm": available["standalone_clang"], "lldb_dap": available["qualified_lldb_dap"], "dwarf_debuggee": available["qualified_lldb_dap"]}
        missing = []
        for entry in TOOL_ACCEPTANCE:
            required = all(aliases.get(capability, False) for capability in entry.required_host_capabilities)
            record = next((item for item in records if item.tool_name == entry.tool_name and item.scenario_id == entry.scenario_id), None)
            if required and (record is None or record.calls <= 0 or not record.meaningful_assertion_completed):
                missing.append(entry.tool_name)
        if missing:
            raise AssertionError("available live capability was skipped or lacked a successful SDK call: " + ", ".join(missing))
        incomplete = [
            record.tool_name for record in records
            if record.calls <= 0 or not record.meaningful_assertion_completed or record.category != "success"
        ]
        if actual != expected or incomplete:
            absent = sorted(tool for tool, _ in expected - actual)
            raise AssertionError(
                f"{len(TOOL_ACCEPTANCE)}-tool SDK coverage is incomplete: " + ", ".join((absent + incomplete)[:len(TOOL_ACCEPTANCE)])
            )
        temporary_report = root / "coverage.json.tmp"
        write_coverage_report(temporary_report)
        temporary_report.replace(report_path)
        session.config._forgemcp_live_report_paths = (discovery_path, report_path)  # type: ignore[attr-defined]
    except AssertionError as error:
        report_path.unlink(missing_ok=True)
        (root / "coverage.json.tmp").unlink(missing_ok=True)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line(f"ForgeMCP unified live acceptance FAILED: {error}")
    else:
        terminal = session.config.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line(f"ForgeMCP live capability report: {discovery_path}")
            terminal.write_line(f"ForgeMCP live SDK coverage report: {report_path}")
