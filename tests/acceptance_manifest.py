"""Version-controlled D2 MCP acceptance inventory."""

from __future__ import annotations

REAL = "real"
OPTIONAL = "optional_platform_gate"

TOOL_ACCEPTANCE: dict[str, str] = {
    "server_status": REAL, "project__status": REAL,
    "workspace__list_files": REAL, "workspace__read_text": REAL,
    "workspace__get_snapshot": REAL, "workspace__apply_unified_patch": REAL,
    "workspace__apply_text_edits": REAL,
    "cmake__status": REAL, "cmake__list_kits": REAL, "cmake__select_kit": REAL,
    "cmake__list_build_trees": REAL, "cmake__list_presets": REAL,
    "cmake__configure": REAL, "cmake__list_targets": REAL, "cmake__build": REAL,
    "cmake__ctest_list_tests": REAL, "cmake__ctest_run": REAL,
    "quality__status": REAL, "clang_format__check": REAL, "clang_format__apply": REAL,
    "clang_tidy__list_checks": REAL, "clang_tidy__run": REAL, "sanitizer__parse_report": REAL,
    **{name: OPTIONAL for name in (
        "clangd__status", "clangd__start", "clangd__stop", "clangd__diagnostics", "clangd__hover",
        "clangd__definition", "clangd__references", "clangd__document_symbols", "clangd__workspace_symbols",
        "clangd__completion", "clangd__signature_help", "clangd__declaration", "clangd__type_definition",
        "clangd__implementation", "clangd__prepare_rename", "clangd__rename", "clangd__code_actions",
        "clangd__apply_code_action", "clangd__format_document", "clangd__format_range",
        "clangd__prepare_call_hierarchy", "clangd__incoming_calls", "clangd__outgoing_calls",
        "clangd__prepare_type_hierarchy", "clangd__supertypes", "clangd__subtypes", "clangd__switch_source_header",
    )},
    **{name: OPTIONAL for name in (
        "debugger__status", "debugger__list_adapters", "debugger__launch", "debugger__stop",
        "debugger__set_breakpoints", "debugger__continue", "debugger__pause", "debugger__step_over",
        "debugger__step_in", "debugger__step_out", "debugger__threads", "debugger__stack_trace",
        "debugger__scopes", "debugger__variables", "debugger__evaluate", "debugger__events",
    )},
}
