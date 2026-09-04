"""Scenario-owned D2 MCP acceptance inventory.

Listing a tool through ``tools/list`` is never coverage. A real SDK scenario
must record its call before the scenario can pass.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from threading import Lock
from typing import Any

from mcp import ClientSession

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
    fixture_anchor: str
    meaningful_success_assertion: str
    required_precondition: str
    cleanup_assertion: str


class McpToolCallCollector:
    """Runtime proof that a scenario crossed the official SDK boundary."""

    def __init__(self, scenario_id: str | Iterable[str]) -> None:
        roots = (scenario_id,) if isinstance(scenario_id, str) else tuple(scenario_id)
        if not roots or any(not root for root in roots):
            raise ValueError("At least one non-empty scenario root is required")
        self.scenario_roots = frozenset(roots)
        self.called_tools: set[str] = set()

    async def call(self, session: object, tool_name: str, arguments: Mapping[str, object] | None = None, **kwargs: object) -> object:
        if not isinstance(session, ClientSession):
            raise AssertionError("MCP coverage requires the official SDK ClientSession")
        call_tool = getattr(session, "call_tool", None)
        if call_tool is None:
            raise AssertionError("MCP scenario has no SDK ClientSession.call_tool boundary")
        # This deliberately wraps the SDK boundary, rather than a ForgeMCP
        # handler or service.  ``tools/list`` is not a coverage event.
        result = await call_tool(tool_name, arguments or {}, **kwargs)
        entry = _manifest_entry(tool_name)
        # Fixture setup is also performed through the public SDK.  Such calls
        # are real integration traffic, but must not manufacture coverage for
        # a different owning scenario (for example CMake setup in clangd).
        if _scenario_root(entry) in self.scenario_roots:
            self.called_tools.add(tool_name)
            _record_sdk_call(self.scenario_roots, tool_name, result, assertion_completed=False)
        return result

    def complete_assertions(self, tool_names: Iterable[str]) -> None:
        """Mark calls only after their scenario's response assertions passed.

        Scenarios invoke this at their successful end.  A parsing or semantic
        assertion failure therefore leaves every affected record incomplete,
        and a manifest declaration alone can never manufacture coverage.
        """
        names = set(tool_names)
        if not names <= self.called_tools:
            raise AssertionError("Cannot complete an assertion for an uncalled MCP tool")
        for tool_name in names:
            _complete_sdk_assertion(self.scenario_roots, tool_name)


@dataclass(frozen=True, slots=True)
class CoverageRecord:
    """One bounded real-MCP observation produced by a named fixture scenario."""

    tool_name: str
    scenario_id: str
    capability_group: str
    calls: int
    meaningful_assertion_completed: bool
    category: str


_coverage_lock = Lock()
_coverage: dict[tuple[str, str], CoverageRecord] = {}


def _result_category(result: object) -> tuple[bool, str]:
    """Classify the public result without retaining response text or paths."""
    if bool(getattr(result, "isError", False)):
        return False, "expected_error"
    try:
        content = getattr(result, "content")
        text = getattr(content[0], "text") if len(content) == 1 else ""
        payload = json.loads(text)
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            return False, "expected_error"
    except (AttributeError, IndexError, TypeError, json.JSONDecodeError):
        # The SDK result itself was successfully received.  Individual
        # scenarios assert its public structure before completing.
        pass
    return True, "success"


def _manifest_entry(tool_name: str) -> ToolAcceptance:
    entry = next((item for item in TOOL_ACCEPTANCE if item.tool_name == tool_name), None)
    if entry is None:
        raise AssertionError(f"SDK acceptance call has no manifest tool: {tool_name}")
    return entry


def _scenario_root(entry: ToolAcceptance) -> str:
    return entry.scenario_id.split(".", 1)[0]


def _owned_manifest_entry(scenario_roots: frozenset[str], tool_name: str) -> ToolAcceptance:
    entry = _manifest_entry(tool_name)
    if _scenario_root(entry) not in scenario_roots:
        raise AssertionError(f"SDK acceptance call crossed the wrong scenario boundary: {tool_name}")
    return entry


def _record_sdk_call(
    scenario_roots: frozenset[str], tool_name: str, result: object, *, assertion_completed: bool
) -> None:
    entry = _owned_manifest_entry(scenario_roots, tool_name)
    meaningful, category = _result_category(result)
    key = (entry.tool_name, entry.scenario_id)
    with _coverage_lock:
        previous = _coverage.get(key)
        _coverage[key] = CoverageRecord(
            tool_name=entry.tool_name,
            scenario_id=entry.scenario_id,
            capability_group=entry.subsystem,
            calls=1 if previous is None else previous.calls + 1,
            meaningful_assertion_completed=assertion_completed or bool(previous and previous.meaningful_assertion_completed),
            category="success" if meaningful or (previous and previous.category == "success") else category,
        )


def _complete_sdk_assertion(scenario_roots: frozenset[str], tool_name: str) -> None:
    entry = _owned_manifest_entry(scenario_roots, tool_name)
    key = (entry.tool_name, entry.scenario_id)
    with _coverage_lock:
        previous = _coverage.get(key)
        if previous is None or previous.calls <= 0:
            raise AssertionError(f"Meaningful assertion has no preceding SDK call: {tool_name}")
        _coverage[key] = CoverageRecord(
            tool_name=previous.tool_name,
            scenario_id=previous.scenario_id,
            capability_group=previous.capability_group,
            calls=previous.calls,
            meaningful_assertion_completed=True,
            category=previous.category,
        )


def coverage_records() -> tuple[CoverageRecord, ...]:
    """Return only aggregate records; no response payload is retained."""
    with _coverage_lock:
        return tuple(sorted(_coverage.values(), key=lambda item: (item.tool_name, item.scenario_id)))


def reset_coverage_records() -> None:
    with _coverage_lock:
        _coverage.clear()


def write_coverage_report(destination: Path) -> None:
    """Write the host-local, bounded machine-readable unified-run evidence."""
    records = coverage_records()
    if len(records) > len(TOOL_ACCEPTANCE):
        raise AssertionError("Coverage has duplicate or orphan records")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "tool_count": len(TOOL_ACCEPTANCE),
        "records": [
            {
                "tool_name": item.tool_name,
                "scenario_id": item.scenario_id,
                "capability_group": item.capability_group,
                "real_mcp_calls": item.calls,
                "meaningful_assertion_completed": item.meaningful_assertion_completed,
                "category": item.category,
            }
            for item in records
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise AssertionError("Coverage report exceeds its fixed bound")
    destination.write_text(encoded + "\n", encoding="utf-8")


def _entries(subsystem: str, tools: Iterable[str], scenario: str, capabilities: frozenset[str], test: str) -> tuple[ToolAcceptance, ...]:
    return tuple(
        ToolAcceptance(
            name, subsystem, f"{scenario}.{name.replace('__', '_')}", capabilities,
            LIVE_MCP, None, test, "public surface", "structured response", "initialized SDK session",
            "MCP transport closes cleanly",
        )
        for name in tools
    )


_CLANGD_DETAILS: dict[str, tuple[str, str, str, str]] = {
    "clangd__status": ("managed session", "reports stopped/running/stopped lifecycle", "SDK initialize", "post-stop state is stopped"),
    "clangd__start": ("CMake-generated build/compile_commands.json", "state is running", "ready standalone LLVM kit selected", "explicit stop succeeds"),
    "clangd__stop": ("managed session", "returns stopped=true", "all semantic calls complete", "server lifespan emits application_stopped"),
    "clangd__diagnostics": ("analysis/code_action.cpp", "current snapshot contains a real error diagnostic", "document synchronized", "no stale diagnostic is returned"),
    "clangd__hover": ("app/good_main.cpp fixture::add", "hover names add/type", "definition readiness barrier passed", "session remains usable"),
    "clangd__definition": ("app/good_main.cpp fixture::shared_value", "location is shared.hpp", "shared header stays closed", "rename later changes that header"),
    "clangd__references": ("fixture::add uses", "multiple workspace references", "TU semantically ready", "all locations remain workspace-relative"),
    "clangd__document_symbols": ("analysis/clangd_anchors.cpp", "contains call_target", "document synchronized", "session remains usable"),
    "clangd__workspace_symbols": ("call_target", "returns call_target workspace symbol", "clangd indexed fixture", "all locations remain workspace-relative"),
    "clangd__completion": ("analysis/clangd_anchors.cpp fixture::", "includes add candidate", "completion anchor synchronized", "no mutation"),
    "clangd__signature_help": ("analysis/clangd_anchors.cpp fixture::add call", "signature has two parameters", "signature anchor synchronized", "no mutation"),
    "clangd__declaration": ("app/good_main.cpp fixture::add", "location is include/fixture/math.hpp", "definition barrier passed", "no mutation"),
    "clangd__type_definition": ("analysis/clangd_anchors.cpp Dog return type", "location is include/fixture/hierarchy.hpp", "TU semantically ready", "no mutation"),
    "clangd__implementation": ("include/fixture/hierarchy.hpp Animal::name", "returns Dog override implementation", "header synchronized", "no mutation"),
    "clangd__prepare_rename": ("app/good_main.cpp fixture::shared_value", "returns rename range and snapshot", "definition barrier passed", "snapshot used for CAS"),
    "clangd__rename": ("shared_value in main/header", "atomic multi-file edit includes shared.hpp", "prepare snapshot/CAS current", "both files contain renamed_shared_value"),
    "clangd__code_actions": ("analysis/code_action.cpp missing semicolon", "returns editable action", "real diagnostics passed", "action handle remains session-bound"),
    "clangd__apply_code_action": ("editable code action handle", "atomic edit is applied", "current action snapshot", "source has terminating semicolon"),
    "clangd__format_document": ("analysis/format_me.cpp", "non-no-op edit applied", "current SHA-256", "formatted file snapshot changes"),
    "clangd__format_range": ("range_format_me range", "range request returns an atomic applied/no-op summary", "fresh SHA-256", "no edit escapes requested file"),
    "clangd__prepare_call_hierarchy": ("call_target", "returns opaque item handle", "call graph anchor indexed", "handle consumed before stop"),
    "clangd__incoming_calls": ("call_target handle", "includes call_source", "prepared live handle", "no stale handle survives mutation/stop"),
    "clangd__outgoing_calls": ("call_source handle", "includes call_target", "prepared live handle", "no stale handle survives mutation/stop"),
    "clangd__prepare_type_hierarchy": ("Dog", "returns opaque type handle", "hierarchy anchor indexed", "handle consumed before stop"),
    "clangd__supertypes": ("Dog handle", "includes Animal", "prepared live handle", "no stale handle survives mutation/stop"),
    "clangd__subtypes": ("Animal handle", "includes Dog", "prepared live handle", "no stale handle survives mutation/stop"),
    "clangd__switch_source_header": ("src/math.cpp", "returns include/fixture/math.hpp", "source/header pair in compilation database", "workspace-relative counterpart only"),
}


def _clangd_entries() -> tuple[ToolAcceptance, ...]:
    return tuple(
        ToolAcceptance(
            name, "clangd", f"clangd_fixture.{name.replace('__', '_')}",
            frozenset({"mcp_stdio", "clangd", "compile_commands"}), LIVE_MCP, None,
            "test_cpp_acceptance_fixture_real_clangd_all_tools_mcp_gate", *_CLANGD_DETAILS[name],
        )
        for name in _CLANGD
    )


_CORE = ("server_status", "project__status", "workspace__list_files", "workspace__read_text", "workspace__get_snapshot", "workspace__apply_unified_patch", "workspace__apply_text_edits")
_CMAKE = ("cmake__status", "cmake__list_kits", "cmake__select_kit", "cmake__list_build_trees", "cmake__list_presets", "cmake__configure", "cmake__list_targets", "cmake__build", "cmake__ctest_list_tests", "cmake__ctest_run")
_QUALITY = ("quality__status", "clang_format__check", "clang_format__apply", "clang_tidy__list_checks", "clang_tidy__run", "sanitizer__parse_report")
_GIT = ("git__status", "git__diff", "git__log", "git__show_commit", "git__blame", "git__list_branches")
MCP_APP_RESOURCE_INVENTORY = (
    {
        "tool_name": "project__status",
        "uri": "ui://forgemcp/project/status",
        "mime_type": "text/html;profile=mcp-app",
    },
    {
        "tool_name": "git__status",
        "uri": "ui://forgemcp/git/status",
        "mime_type": "text/html;profile=mcp-app",
    },
)
_CLANGD = ("clangd__status", "clangd__start", "clangd__stop", "clangd__diagnostics", "clangd__hover", "clangd__definition", "clangd__references", "clangd__document_symbols", "clangd__workspace_symbols", "clangd__completion", "clangd__signature_help", "clangd__declaration", "clangd__type_definition", "clangd__implementation", "clangd__prepare_rename", "clangd__rename", "clangd__code_actions", "clangd__apply_code_action", "clangd__format_document", "clangd__format_range", "clangd__prepare_call_hierarchy", "clangd__incoming_calls", "clangd__outgoing_calls", "clangd__prepare_type_hierarchy", "clangd__supertypes", "clangd__subtypes", "clangd__switch_source_header")
_DEBUGGER = ("debugger__status", "debugger__list_adapters", "debugger__launch", "debugger__stop", "debugger__set_breakpoints", "debugger__continue", "debugger__pause", "debugger__step_over", "debugger__step_in", "debugger__step_out", "debugger__threads", "debugger__stack_trace", "debugger__scopes", "debugger__variables", "debugger__evaluate", "debugger__events")

_DEBUGGER_DETAILS: dict[str, tuple[str, str, str, str]] = {
    "debugger__status": ("managed fixture session", "observes stopped/running/paused/terminated generations", "initialized SDK session", "application shutdown returns no active session"),
    "debugger__list_adapters": ("production discovery", "qualified standalone LLVM LLDB-DAP is available with path-free metadata", "initialized SDK session", "no adapter starts during discovery"),
    "debugger__launch": ("fixture_debug PE/COFF + DWARF", "launch reaches a managed paused/running state", "selected standalone Clang kit and CMake-built target", "stop removes the test-owned adapter tree"),
    "debugger__stop": ("paused and running fixture sessions", "idempotent stop returns terminated with a terminal event", "active managed session", "adapter/debuggee ownership is released"),
    "debugger__set_breakpoints": ("debug/debug_main.cpp FIXTURE_STEP_OVER_MARKER", "adapter verifies and reaches the source breakpoint", "launched entry-stop session", "breakpoint handles do not survive a new session"),
    "debugger__continue": ("main -> debug_middle flow", "changes stop generation and reaches the configured breakpoint", "paused entry-stop session", "stale paused handles are rejected"),
    "debugger__pause": ("debug_bounded_running", "RUNNING becomes PAUSED with a pause event", "bounded-running debuggee", "stop from PAUSED succeeds"),
    "debugger__step_over": ("FIXTURE_STEP_OVER_MARKER", "source location advances to the call anchor", "paused breakpoint frame", "prior frame handles become stale"),
    "debugger__step_in": ("FIXTURE_STEP_IN_MARKER", "top workspace frame enters debug_middle", "paused main call anchor", "prior thread/frame handles become stale"),
    "debugger__step_out": ("debug_middle", "top workspace frame returns to main", "paused debug_middle frame", "prior thread/frame handles become stale"),
    "debugger__threads": ("paused fixture breakpoint", "returns an opaque current stopped thread", "stopped event observed", "thread handle expires on resume/stop"),
    "debugger__stack_trace": ("paused fixture breakpoint", "contains workspace debug_main.cpp frame", "opaque current thread", "frame handle expires on resume/stop"),
    "debugger__scopes": ("main paused frame", "returns a scope with opaque variables handle", "opaque current frame", "scope/variables handles expire on resume"),
    "debugger__variables": ("main seed local", "Variables contains seed with value 40", "opaque current variables handle", "stale variables read is structured"),
    "debugger__evaluate": ("main seed local", "hover identifier lookup returns 40 and policy rejects expressions", "opaque current frame", "no evaluation handle survives resume"),
    "debugger__events": ("full fixture lifecycle", "cursor ordering includes stopped/continued/terminal events", "managed session events", "terminal event is cleared only at application shutdown"),
}


def _debugger_entries() -> tuple[ToolAcceptance, ...]:
    return tuple(
        ToolAcceptance(
            name, "debugger", f"debugger_fixture.{name.replace('__', '_')}",
            frozenset({"mcp_stdio", "standalone_llvm", "lldb_dap", "dwarf_debuggee"}), LIVE_MCP, None,
            "test_cpp_acceptance_fixture_real_debugger_all_tools_mcp_gate", *_DEBUGGER_DETAILS[name],
        )
        for name in _DEBUGGER
    )

TOOL_ACCEPTANCE = (
    _entries("core", _CORE, "core_fixture", frozenset({"mcp_stdio"}), "test_cpp_acceptance_fixture_mcp_surface_and_real_cmake_workspace_quality_flow")
    + _entries("cmake", _CMAKE, "cmake_fixture", frozenset({"mcp_stdio", "cmake", "ninja"}), "test_cpp_acceptance_fixture_mcp_surface_and_real_cmake_workspace_quality_flow")
    + _entries("quality", _QUALITY, "quality_fixture", frozenset({"mcp_stdio", "cmake", "ninja"}), "test_cpp_acceptance_fixture_mcp_surface_and_real_cmake_workspace_quality_flow")
    + _entries("git", _GIT, "git_fixture", frozenset({"mcp_stdio", "git"}), "test_git_mcp_sdk_disposable_repository_all_six_tools")
    + _clangd_entries()
    + _debugger_entries()
)


def validate_manifest(public_tools: Iterable[str], *, registered_scenarios: Iterable[str], observed_calls: Mapping[str, Iterable[str]] | None = None, available_capabilities: frozenset[str] = frozenset(), skipped_scenarios: Mapping[str, str] | None = None, subsystems: frozenset[str] | None = None) -> None:
    """Fail closed for inventory drift, false skips, and fake coverage claims."""
    listed = tuple(public_tools)
    selected = tuple(entry for entry in TOOL_ACCEPTANCE if subsystems is None or entry.subsystem in subsystems)
    entries = {entry.tool_name: entry for entry in selected}
    if len(entries) != len(selected) or set(entries) != set(listed):
        raise AssertionError("Every and only public MCP tool must have one manifest entry")
    registered, calls, skips = set(registered_scenarios), {key: set(value) for key, value in (observed_calls or {}).items()}, dict(skipped_scenarios or {})
    for entry in selected:
        if not entry.scenario_id or not entry.test_scenario or entry.scenario_id not in registered:
            raise AssertionError(f"Manifest scenario is not registered: {entry.scenario_id}")
        if entry.subsystem in {"clangd", "debugger"} and not all((
            entry.fixture_anchor, entry.meaningful_success_assertion,
            entry.required_precondition, entry.cleanup_assertion,
        )):
            raise AssertionError(f"Clangd tool lacks acceptance details: {entry.tool_name}")
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
