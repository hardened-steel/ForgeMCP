"""Scenario-owned D2 MCP acceptance inventory.

Listing a tool through ``tools/list`` is never coverage. A real SDK scenario
must record its call before the scenario can pass.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

LIVE_MCP = "live_mcp"
FAKE_MCP = "fake_mcp"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ToolAcceptance:
    tool_name: str
    subsystem: str
    scenario_id: str
    required_host_capabilities: frozenset[str]
    coverage_tier: str
    unavailable_reason: str | None
    test_scenario: str


class McpToolCallCollector:
    """Runtime proof that a scenario crossed the official SDK boundary."""

    def __init__(self, scenario_id: str) -> None:
        self.scenario_id = scenario_id
        self.called_tools: set[str] = set()

    async def call(self, session: object, tool_name: str, arguments: Mapping[str, object] | None = None, **kwargs: object) -> object:
        call_tool = getattr(session, "call_tool", None)
        if call_tool is None:
            raise AssertionError("MCP scenario has no SDK ClientSession.call_tool boundary")
        self.called_tools.add(tool_name)
        return await call_tool(tool_name, arguments or {}, **kwargs)


def _entries(subsystem: str, tools: Iterable[str], scenario: str, capabilities: frozenset[str], test: str) -> tuple[ToolAcceptance, ...]:
    return tuple(ToolAcceptance(name, subsystem, f"{scenario}.{name.replace('__', '_')}", capabilities, LIVE_MCP, None, test) for name in tools)


_CORE = ("server_status", "project__status", "workspace__list_files", "workspace__read_text", "workspace__get_snapshot", "workspace__apply_unified_patch", "workspace__apply_text_edits")
_CMAKE = ("cmake__status", "cmake__list_kits", "cmake__select_kit", "cmake__list_build_trees", "cmake__list_presets", "cmake__configure", "cmake__list_targets", "cmake__build", "cmake__ctest_list_tests", "cmake__ctest_run")
_QUALITY = ("quality__status", "clang_format__check", "clang_format__apply", "clang_tidy__list_checks", "clang_tidy__run", "sanitizer__parse_report")
_CLANGD = ("clangd__status", "clangd__start", "clangd__stop", "clangd__diagnostics", "clangd__hover", "clangd__definition", "clangd__references", "clangd__document_symbols", "clangd__workspace_symbols", "clangd__completion", "clangd__signature_help", "clangd__declaration", "clangd__type_definition", "clangd__implementation", "clangd__prepare_rename", "clangd__rename", "clangd__code_actions", "clangd__apply_code_action", "clangd__format_document", "clangd__format_range", "clangd__prepare_call_hierarchy", "clangd__incoming_calls", "clangd__outgoing_calls", "clangd__prepare_type_hierarchy", "clangd__supertypes", "clangd__subtypes", "clangd__switch_source_header")
_DEBUGGER = ("debugger__status", "debugger__list_adapters", "debugger__launch", "debugger__stop", "debugger__set_breakpoints", "debugger__continue", "debugger__pause", "debugger__step_over", "debugger__step_in", "debugger__step_out", "debugger__threads", "debugger__stack_trace", "debugger__scopes", "debugger__variables", "debugger__evaluate", "debugger__events")

TOOL_ACCEPTANCE = (
    _entries("core", _CORE, "core_fixture", frozenset({"mcp_stdio"}), "test_cpp_acceptance_fixture_mcp_surface_and_real_cmake_workspace_quality_flow")
    + _entries("cmake", _CMAKE, "cmake_fixture", frozenset({"mcp_stdio", "cmake", "ninja"}), "test_cpp_acceptance_fixture_mcp_surface_and_real_cmake_workspace_quality_flow")
    + _entries("quality", _QUALITY, "quality_fixture", frozenset({"mcp_stdio", "cmake", "ninja"}), "test_cpp_acceptance_fixture_mcp_surface_and_real_cmake_workspace_quality_flow")
    + _entries("clangd", _CLANGD, "clangd_fixture", frozenset({"mcp_stdio", "clangd", "compile_commands"}), "test_stdio_mcp_real_clangd_rename_stop_and_transport_shutdown")
    + _entries("debugger", _DEBUGGER, "debugger_fixture", frozenset({"mcp_stdio", "standalone_llvm", "lldb_dap"}), "test_stdio_mcp_real_debugger_fixture")
)


def validate_manifest(public_tools: Iterable[str], *, registered_scenarios: Iterable[str], observed_calls: Mapping[str, Iterable[str]] | None = None, available_capabilities: frozenset[str] = frozenset(), skipped_scenarios: Mapping[str, str] | None = None) -> None:
    """Fail closed for inventory drift, false skips, and fake coverage claims."""
    listed = tuple(public_tools)
    entries = {entry.tool_name: entry for entry in TOOL_ACCEPTANCE}
    if len(entries) != len(TOOL_ACCEPTANCE) or set(entries) != set(listed):
        raise AssertionError("Every and only public MCP tool must have one manifest entry")
    registered, calls, skips = set(registered_scenarios), {key: set(value) for key, value in (observed_calls or {}).items()}, dict(skipped_scenarios or {})
    for entry in TOOL_ACCEPTANCE:
        if not entry.scenario_id or not entry.test_scenario or entry.scenario_id not in registered:
            raise AssertionError(f"Manifest scenario is not registered: {entry.scenario_id}")
        if entry.coverage_tier not in {LIVE_MCP, FAKE_MCP, UNAVAILABLE}:
            raise AssertionError(f"Invalid coverage tier: {entry.tool_name}")
        if entry.coverage_tier == UNAVAILABLE and not entry.unavailable_reason:
            raise AssertionError(f"Unavailable tool requires a reason: {entry.tool_name}")
        missing = entry.required_host_capabilities - available_capabilities
        if entry.scenario_id in skips:
            if not missing or not skips[entry.scenario_id]:
                raise AssertionError(f"Available or unreasoned scenario was skipped: {entry.scenario_id}")
        elif observed_calls is not None and entry.tool_name not in calls.get(entry.scenario_id, set()):
            raise AssertionError(f"Scenario did not call its declared MCP tool: {entry.tool_name}")
