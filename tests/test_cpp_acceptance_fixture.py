"""D2 fixture structure and real portable MCP acceptance gates.

The live tier automatically runs when CMake/Ninja exist.  Every write is made
below a pytest-owned copy of the committed fixture.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from hashlib import sha256

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.acceptance_manifest import McpToolCallCollector, TOOL_ACCEPTANCE, validate_manifest


FIXTURE_ROOT = Path(__file__).parents[1] / "examples" / "cpp-acceptance-project"


def _copy_fixture(destination: Path) -> Path:
    copied = destination / "cpp-acceptance-project"
    shutil.copytree(FIXTURE_ROOT, copied)
    return copied


def _json(result: object) -> dict[str, object]:
    content = getattr(result, "content")
    assert len(content) == 1
    payload = json.loads(getattr(content[0], "text"))
    assert isinstance(payload, dict)
    return payload


def _portable_prerequisites() -> bool:
    return all(shutil.which(tool) for tool in ("cmake", "ninja"))


def _tree_hashes(root: Path) -> dict[str, str]:
    """Content proof that no real gate modifies the committed fixture."""
    return {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _position(text: str, needle: str, *, offset: int = 0) -> dict[str, int]:
    """Return the public code-point position at a fixture anchor."""
    index = text.index(needle) + offset
    before = text[:index]
    return {"line": before.count("\n"), "column": len(before.rsplit("\n", 1)[-1])}


def _snapshot_sha(payload: dict[str, object]) -> str:
    snapshot = payload["snapshot"]
    assert isinstance(snapshot, dict)
    value = snapshot["sha256"]
    assert isinstance(value, str)
    return value


def test_cpp_acceptance_fixture_is_complete_and_has_no_generated_artifacts() -> None:
    required = {
        "CMakeLists.txt", "CMakePresets.json", "README.md", "include/fixture/math.hpp",
        "include/fixture/hierarchy.hpp", "src/math.cpp", "src/hierarchy.cpp",
        "shared.hpp", "analysis/clangd_anchors.cpp",
        "app/good_main.cpp", "app/warning_main.cpp", "negative/compile_error.cpp",
        "negative/link_error.cpp", "analysis/code_action.cpp", "analysis/format_me.cpp",
        "analysis/tidy_me.cpp", "debug/debug_main.cpp", "tests/test_main.cpp",
        "reports/asan.txt", "reports/ubsan.txt", "reports/external-frame.txt",
        "reports/malformed.txt", "reports/truncated.txt",
    }
    present = {path.relative_to(FIXTURE_ROOT).as_posix() for path in FIXTURE_ROOT.rglob("*") if path.is_file()}
    assert required <= present
    assert not any(
        path.suffix.lower() in {".pdb", ".exe"} or path.name == "compile_commands.json"
        or any(part in {"build", ".cache"} or part.startswith("build-") for part in path.relative_to(FIXTURE_ROOT).parts)
        for path in FIXTURE_ROOT.rglob("*")
    )
    cmake = (FIXTURE_ROOT / "CMakeLists.txt").read_text(encoding="utf-8")
    for target in ("fixture_core", "fixture_good", "fixture_warning", "fixture_compile_error", "fixture_link_error", "fixture_debug"):
        assert target in cmake
    assert "EXCLUDE_FROM_ALL" in cmake
    assert "WILL_FAIL" in cmake
    assert "FIXTURE_ENABLE_NEGATIVE_TESTS" in cmake
    assert "FIXTURE_BREAKPOINT_MARKER" in (FIXTURE_ROOT / "debug/debug_main.cpp").read_text(encoding="utf-8")
    assert "RANGE_FORMAT_BEGIN" in (FIXTURE_ROOT / "analysis/format_me.cpp").read_text(encoding="utf-8")
    assert "shared_value" in (FIXTURE_ROOT / "shared.hpp").read_text(encoding="utf-8")


@pytest.mark.skipif(not _portable_prerequisites(), reason="real portable D2 gate requires installed CMake and Ninja")
def test_cpp_acceptance_fixture_real_cmake_gate_uses_a_disposable_copy(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)
    build = root / "build"

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=root, text=True, capture_output=True, timeout=120, check=False)

    configured = run("cmake", "-S", str(root), "-B", str(build), "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON")
    assert configured.returncode == 0, configured.stderr[-2000:]
    built = run("cmake", "--build", str(build))
    assert built.returncode == 0, built.stderr[-2000:]
    assert (build / "compile_commands.json").is_file()
    tested = run("ctest", "--test-dir", str(build), "--output-on-failure")
    assert tested.returncode == 0, tested.stdout[-2000:]
    warning = run("cmake", "--build", str(build), "--target", "fixture_warning", "--clean-first")
    assert warning.returncode == 0
    assert "fixture warning" in (warning.stdout + warning.stderr)
    compile_failure = run("cmake", "--build", str(build), "--target", "fixture_compile_error")
    assert compile_failure.returncode != 0
    assert "fixture_undeclared_identifier" in (compile_failure.stdout + compile_failure.stderr)
    link_failure = run("cmake", "--build", str(build), "--target", "fixture_link_error")
    assert link_failure.returncode != 0
    assert "intentionally_undefined_symbol" in (link_failure.stdout + link_failure.stderr)
    assert not (FIXTURE_ROOT / "build").exists()


@pytest.mark.skipif(not _portable_prerequisites(), reason="real portable D2 MCP gate requires installed CMake and Ninja")
def test_cpp_acceptance_fixture_mcp_surface_and_real_cmake_workspace_quality_flow(tmp_path: Path) -> None:
    root = _copy_fixture(tmp_path)

    async def exercise() -> None:
        errors = tmp_path / "server-stderr.log"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "forgemcp.server"],
            cwd=Path.cwd(),
            env={**os.environ, "FORGEMCP_WORKSPACE": str(root), "FORGEMCP_LOG_LEVEL": "INFO"},
        )
        with errors.open("w", encoding="utf-8") as stderr:
            async with stdio_client(parameters, errlog=stderr) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    names = [tool.name for tool in (await session.list_tools()).tools]
                    assert len(names) == len(set(names))
                    assert set(names) == {entry.tool_name for entry in TOOL_ACCEPTANCE}
                    visited: set[str] = set()

                    async def call(name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
                        visited.add(name)
                        return _json(await session.call_tool(name, arguments or {}))

                    await call("server_status")
                    await call("project__status")
                    await call("workspace__list_files", {"path": ".", "recursive": True})
                    before = await call("workspace__read_text", {"path": "analysis/format_me.cpp"})
                    snapshot = before["snapshot"]["sha256"]  # type: ignore[index]
                    await call("workspace__get_snapshot", {"path": "analysis/format_me.cpp"})
                    created = await call("workspace__apply_unified_patch", {
                        "patch": "--- /dev/null\n+++ b/analysis/created.cpp\n@@ -0,0 +1 @@\n+int created = 1;\n",
                        "expected_snapshots": {"analysis/created.cpp": None},
                    })
                    assert created["applied"] is True
                    edited = await call("workspace__apply_text_edits", {
                        "edits_by_path": {"analysis/format_me.cpp": [{"range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 0}}, "new_text": "// D2\n"}]},
                        "expected_snapshots": {"analysis/format_me.cpp": snapshot},
                    })
                    assert edited["applied"] is True
                    stale = await call("workspace__apply_text_edits", {
                        "edits_by_path": {"analysis/format_me.cpp": [{"range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 0}}, "new_text": "// stale\n"}]},
                        "expected_snapshots": {"analysis/format_me.cpp": snapshot},
                    })
                    assert stale["applied"] is False

                    await call("cmake__status")
                    kits = await call("cmake__list_kits")
                    ready_kits = [item for item in kits["kits"] if item["readiness"] == "ready"]  # type: ignore[index]
                    assert ready_kits
                    # The portable live flow uses the standalone LLVM kit.
                    # MSVC/Developer-environment behaviour remains covered by
                    # its dedicated Windows gate rather than conflating the
                    # two qualified workflows.
                    preferred_kit = next((item for item in ready_kits if item["compiler_family"] == "clang"), ready_kits[0])
                    await call("cmake__select_kit", {
                        "kit": preferred_kit["id"], "expected_selection_generation": 0,
                    })
                    await call("cmake__list_build_trees")
                    await call("cmake__list_presets")
                    configured = await call("cmake__configure", {
                        "binary_dir": "build", "generator": "Ninja",
                        "cache_variables": {"CMAKE_BUILD_TYPE": "Debug"},
                    })
                    assert configured["process"]["exit_code"] == 0  # type: ignore[index]
                    await call("cmake__list_targets", {"binary_dir": "build"})
                    warning = await call("cmake__build", {"binary_dir": "build", "targets": ["fixture_warning"]})
                    assert warning["process"]["exit_code"] == 0  # type: ignore[index]
                    built = await call("cmake__build", {"binary_dir": "build"})
                    assert built["process"]["exit_code"] == 0, json.dumps(built["process"], indent=2)  # type: ignore[index]
                    compile_failure = await call("cmake__build", {"binary_dir": "build", "targets": ["fixture_compile_error"]})
                    assert compile_failure["process"]["exit_code"] != 0  # type: ignore[index]
                    assert compile_failure["diagnostics"]  # type: ignore[index]
                    link_failure = await call("cmake__build", {"binary_dir": "build", "targets": ["fixture_link_error"]})
                    assert link_failure["process"]["exit_code"] != 0  # type: ignore[index]
                    tests = await call("cmake__ctest_list_tests", {"binary_dir": "build"})
                    assert {item["name"] for item in tests["tests"]} == {"fixture_pass", "fixture_expected_failure"}  # type: ignore[index]
                    passed = await call("cmake__ctest_run", {"binary_dir": "build"})
                    assert passed["process"]["exit_code"] == 0  # type: ignore[index]
                    negative_configure = await call("cmake__configure", {
                        "binary_dir": "build-negative", "generator": "Ninja",
                        "cache_variables": {"CMAKE_BUILD_TYPE": "Debug", "FIXTURE_ENABLE_NEGATIVE_TESTS": True},
                    })
                    assert negative_configure["process"]["exit_code"] == 0  # type: ignore[index]
                    negative_build = await call("cmake__build", {"binary_dir": "build-negative", "targets": ["fixture_tests"]})
                    assert negative_build["process"]["exit_code"] == 0  # type: ignore[index]
                    negative_tests = await call("cmake__ctest_list_tests", {"binary_dir": "build-negative"})
                    assert {item["name"] for item in negative_tests["tests"]} >= {"fixture_intentional_failure", "fixture_timeout"}  # type: ignore[index]
                    failing_test = await call("cmake__ctest_run", {"binary_dir": "build-negative", "test_names": ["fixture_intentional_failure"]})
                    assert failing_test["test_names"] == ["fixture_intentional_failure"]
                    timeout_test = await call("cmake__ctest_run", {"binary_dir": "build-negative", "test_names": ["fixture_timeout"], "timeout_seconds": 3})
                    assert timeout_test["test_names"] == ["fixture_timeout"]
                    conflict = await call("cmake__configure", {"binary_dir": "build-conflict", "preset": "ninja-debug"})
                    assert conflict["error"]["code"] == "preset_kit_conflict"  # type: ignore[index]

                    await call("quality__status")
                    checked = await call("clang_format__check", {"paths": ["analysis/format_me.cpp"]})
                    item = checked["files"][0]  # type: ignore[index]
                    assert item["would_change"] is True
                    applied = await call("clang_format__apply", {"files": [{"path": item["path"], "expected_sha256": item["snapshot_sha256"]}]})
                    assert applied["applied"] is True
                    await call("clang_tidy__list_checks", {"checks": "modernize-use-nullptr"})
                    await call("clang_tidy__run", {"paths": ["analysis/tidy_me.cpp"], "compile_commands_dir": "build", "checks": "-*,modernize-use-nullptr"})
                    report = (root / "reports" / "asan.txt").read_text(encoding="utf-8")
                    await call("sanitizer__parse_report", {"output": report})
                    mandatory = {
                        entry.tool_name for entry in TOOL_ACCEPTANCE
                        if entry.subsystem in {"core", "cmake", "quality"}
                    }
                    assert mandatory <= visited
        text = errors.read_text(encoding="utf-8")
        assert str(root) not in text

    asyncio.run(exercise())


@pytest.mark.clangd_fixture_mcp
@pytest.mark.skipif(not _portable_prerequisites(), reason="real clangd fixture gate requires CMake and Ninja")
def test_cpp_acceptance_fixture_real_clangd_all_tools_mcp_gate(tmp_path: Path) -> None:
    """Call every live ``clangd__*`` tool through the official SDK stdio client.

    This deliberately performs production-equivalent tool/kit discovery before
    it can skip.  A qualified clangd and standalone LLVM kit therefore make a
    skip impossible; the test must complete against the disposable fixture.
    """
    committed_before = _tree_hashes(FIXTURE_ROOT)
    root = _copy_fixture(tmp_path)

    async def exercise() -> str:
        errors = tmp_path / "clangd-server-stderr.log"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "forgemcp.server"],
            cwd=Path.cwd(),
            env={**os.environ, "FORGEMCP_WORKSPACE": str(root), "FORGEMCP_LOG_LEVEL": "INFO"},
        )
        with errors.open("w", encoding="utf-8") as stderr:
            async with stdio_client(parameters, errlog=stderr) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    clangd_tools = {tool.name for tool in listed.tools if tool.name.startswith("clangd__")}
                    manifest_tools = {entry.tool_name for entry in TOOL_ACCEPTANCE if entry.subsystem == "clangd"}
                    assert clangd_tools == manifest_tools
                    assert len(clangd_tools) == 27

                    collector = McpToolCallCollector("clangd_fixture")

                    async def call(name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
                        result = await collector.call(session, name, arguments)
                        assert getattr(result, "isError") is False, (name, [getattr(item, "text", "") for item in getattr(result, "content")])
                        return _json(result)

                    initial_status = await call("clangd__status")
                    kits = await call("cmake__list_kits")
                    available = initial_status.get("available") is True
                    kit_values = kits.get("kits")
                    assert isinstance(kit_values, list)
                    standalone = next(
                        (
                            kit for kit in kit_values
                            if isinstance(kit, dict)
                            and kit.get("readiness") == "ready"
                            and kit.get("origin") == "standalone"
                            and kit.get("compiler_family") == "clang"
                        ),
                        None,
                    )
                    if not available or standalone is None:
                        pytest.skip(
                            "clangd_fixture_unavailable: production discovery found no qualified clangd/standalone LLVM kit"
                        )
                    kit_id = standalone.get("id")
                    assert isinstance(kit_id, str)
                    selected = await call("cmake__select_kit", {
                        "kit": kit_id, "expected_selection_generation": 0,
                    })
                    assert selected.get("selection_generation") == 1
                    configured = await call("cmake__configure", {
                        "binary_dir": "build", "generator": "Ninja",
                        "cache_variables": {"CMAKE_BUILD_TYPE": "Debug"},
                    })
                    process = configured.get("process")
                    database = configured.get("compilation_database")
                    assert isinstance(process, dict) and process.get("exit_code") == 0
                    assert isinstance(database, dict) and database.get("availability") == "available"

                    started = await call("clangd__start")
                    status = started.get("status")
                    assert isinstance(status, dict) and status.get("state") == "running"

                    good_main = (root / "app/good_main.cpp").read_text(encoding="utf-8")
                    anchors = (root / "analysis/clangd_anchors.cpp").read_text(encoding="utf-8")
                    hierarchy = (root / "include/fixture/hierarchy.hpp").read_text(encoding="utf-8")
                    code_action_text = (root / "analysis/code_action.cpp").read_text(encoding="utf-8")
                    format_text = (root / "analysis/format_me.cpp").read_text(encoding="utf-8")
                    shared_position = _position(good_main, "shared_value")
                    add_position = _position(good_main, "fixture::add", offset=len("fixture::"))

                    # A definition in the intentionally unopened header is the
                    # semantic readiness barrier.  No sleep/retry loop masks an
                    # indexing race here.
                    definition = await call("clangd__definition", {
                        "path": "app/good_main.cpp", "position": shared_position,
                    })
                    locations = definition.get("locations")
                    assert isinstance(locations, list)
                    assert any(isinstance(item, dict) and item.get("path") == "shared.hpp" for item in locations)

                    prepared = await call("clangd__prepare_rename", {
                        "path": "app/good_main.cpp", "position": shared_position,
                    })
                    assert prepared.get("range") is not None
                    rejected = await call("clangd__rename", {
                        "path": "app/good_main.cpp", "position": shared_position,
                        "new_name": "renamed_shared_value", "expected_sha256": "0" * 64,
                    })
                    assert rejected.get("error", {}).get("code") == "clangd_edit_conflict"
                    renamed = await call("clangd__rename", {
                        "path": "app/good_main.cpp", "position": shared_position,
                        "new_name": "renamed_shared_value", "expected_sha256": _snapshot_sha(prepared),
                    })
                    rename_edit = renamed.get("edit")
                    assert isinstance(rename_edit, dict) and rename_edit.get("applied") is True
                    assert rename_edit.get("affected_files") == 2
                    assert "renamed_shared_value" in (root / "app/good_main.cpp").read_text(encoding="utf-8")
                    assert "renamed_shared_value" in (root / "shared.hpp").read_text(encoding="utf-8")

                    code_diagnostics = await call("clangd__diagnostics", {
                        "path": "analysis/code_action.cpp", "timeout_seconds": 15.0,
                    })
                    diagnostics = code_diagnostics.get("diagnostics")
                    assert isinstance(diagnostics, list) and diagnostics
                    assert code_diagnostics.get("complete") is True and code_diagnostics.get("stale") is False
                    assert any(item.get("severity") == "error" for item in diagnostics if isinstance(item, dict))

                    hover = await call("clangd__hover", {"path": "app/good_main.cpp", "position": add_position})
                    assert isinstance(hover.get("contents"), str) and "add" in hover["contents"]
                    declaration = await call("clangd__declaration", {"path": "app/good_main.cpp", "position": add_position})
                    assert any(item.get("path") == "include/fixture/math.hpp" for item in declaration["locations"] if isinstance(item, dict))
                    # Open a second real use through the public synchronization
                    # path before asking clangd for cross-document references.
                    formatted_diagnostics = await call("clangd__diagnostics", {
                        "path": "analysis/format_me.cpp", "timeout_seconds": 15.0,
                    })
                    assert formatted_diagnostics.get("complete") is True and formatted_diagnostics.get("stale") is False
                    references = await call("clangd__references", {
                        "path": "app/good_main.cpp", "position": add_position, "include_declaration": True,
                    })
                    assert len(references.get("locations", [])) >= 2
                    document_symbols = await call("clangd__document_symbols", {"path": "analysis/clangd_anchors.cpp"})
                    assert any(item.get("name") == "call_target" for item in document_symbols.get("symbols", []) if isinstance(item, dict)), document_symbols
                    workspace_symbols = await call("clangd__workspace_symbols", {"query": "call_target", "limit": 20})
                    assert any(item.get("name") == "call_target" for item in workspace_symbols.get("symbols", []) if isinstance(item, dict))
                    completion = await call("clangd__completion", {
                        "path": "analysis/clangd_anchors.cpp",
                        "position": _position(anchors, "fixture::", offset=len("fixture::")), "limit": 100,
                    })
                    assert any("add" in str(item.get("label", "")) for item in completion.get("items", []) if isinstance(item, dict)), completion
                    signature = await call("clangd__signature_help", {
                        "path": "analysis/clangd_anchors.cpp",
                        "position": _position(anchors, "fixture::add(1,", offset=len("fixture::add(1,")),
                    })
                    assert any(len(item.get("parameters", [])) == 2 for item in signature.get("signatures", []) if isinstance(item, dict))
                    type_definition = await call("clangd__type_definition", {
                        "path": "analysis/clangd_anchors.cpp", "position": _position(anchors, "Dog type_anchor"),
                    })
                    assert any(item.get("path") == "include/fixture/hierarchy.hpp" for item in type_definition.get("locations", []) if isinstance(item, dict))
                    implementation = await call("clangd__implementation", {
                        "path": "include/fixture/hierarchy.hpp", "position": _position(hierarchy, "name() const"),
                    })
                    assert any(item.get("path") == "include/fixture/hierarchy.hpp" for item in implementation.get("locations", []) if isinstance(item, dict)), implementation

                    call_target = await call("clangd__prepare_call_hierarchy", {
                        "path": "analysis/clangd_anchors.cpp", "position": _position(anchors, "call_target"),
                    })
                    target_item = next(item for item in call_target.get("items", []) if isinstance(item, dict) and item.get("name") == "call_target")
                    target_id = target_item.get("item_id")
                    assert isinstance(target_id, str)
                    incoming = await call("clangd__incoming_calls", {"item_id": target_id, "limit": 20})
                    assert any(item.get("from_item", {}).get("name") == "call_source" for item in incoming.get("calls", []) if isinstance(item, dict))
                    call_source = await call("clangd__prepare_call_hierarchy", {
                        "path": "analysis/clangd_anchors.cpp", "position": _position(anchors, "call_source"),
                    })
                    source_item = next(item for item in call_source.get("items", []) if isinstance(item, dict) and item.get("name") == "call_source")
                    source_id = source_item.get("item_id")
                    assert isinstance(source_id, str)
                    outgoing = await call("clangd__outgoing_calls", {"item_id": source_id, "limit": 20})
                    assert any(item.get("to_item", {}).get("name") == "call_target" for item in outgoing.get("calls", []) if isinstance(item, dict))
                    dog_type = await call("clangd__prepare_type_hierarchy", {
                        "path": "include/fixture/hierarchy.hpp", "position": _position(hierarchy, "Dog final"),
                    })
                    dog_item = next(item for item in dog_type.get("items", []) if isinstance(item, dict) and item.get("name") == "Dog")
                    dog_id = dog_item.get("item_id")
                    assert isinstance(dog_id, str)
                    supers = await call("clangd__supertypes", {"item_id": dog_id, "limit": 20})
                    assert any(item.get("name") == "Animal" for item in supers.get("items", []) if isinstance(item, dict))
                    animal_type = await call("clangd__prepare_type_hierarchy", {
                        "path": "include/fixture/hierarchy.hpp", "position": _position(hierarchy, "Animal {"),
                    })
                    animal_item = next(item for item in animal_type.get("items", []) if isinstance(item, dict) and item.get("name") == "Animal")
                    animal_id = animal_item.get("item_id")
                    assert isinstance(animal_id, str)
                    subs = await call("clangd__subtypes", {"item_id": animal_id, "limit": 20})
                    assert any(item.get("name") == "Dog" for item in subs.get("items", []) if isinstance(item, dict))
                    switched = await call("clangd__switch_source_header", {"path": "src/math.cpp"})
                    assert switched.get("path") == "include/fixture/math.hpp"

                    actions = await call("clangd__code_actions", {
                        "path": "analysis/code_action.cpp",
                        "range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": len(code_action_text.splitlines()[0])}},
                        "diagnostics": diagnostics, "limit": 20,
                    })
                    editable = next(
                        item for item in actions.get("actions", [])
                        if isinstance(item, dict) and item.get("command_only") is False
                        and (item.get("has_workspace_edit") is True or item.get("requires_resolve") is True)
                    )
                    action_id = editable.get("action_id")
                    assert isinstance(action_id, str)
                    applied_action = await call("clangd__apply_code_action", {
                        "action_id": action_id, "expected_sha256": _snapshot_sha(actions),
                    })
                    assert applied_action.get("applied") is True and (root / "analysis/code_action.cpp").read_text(encoding="utf-8").splitlines()[0].endswith(";")

                    range_snapshot = await call("workspace__get_snapshot", {"path": "analysis/format_me.cpp"})
                    range_format = await call("clangd__format_range", {
                        "path": "analysis/format_me.cpp", "expected_sha256": _snapshot_sha(range_snapshot),
                        "range": {"start": {"line": 3, "column": 0}, "end": {"line": 3, "column": len(format_text.splitlines()[3])}},
                    })
                    # A conforming clangd may legitimately regard the marked
                    # range as canonical; the public no-op summary is the
                    # meaningful assertion for this range-format request.
                    assert range_format.get("edit", {}).get("applied") is True
                    document_snapshot = await call("workspace__get_snapshot", {"path": "analysis/format_me.cpp"})
                    document_format = await call("clangd__format_document", {
                        "path": "analysis/format_me.cpp", "expected_sha256": _snapshot_sha(document_snapshot),
                    })
                    assert document_format.get("edit", {}).get("applied") is True and document_format["edit"].get("no_op") is False

                    stopped = await call("clangd__stop")
                    assert stopped == {"stopped": True}
                    final_status = await call("clangd__status")
                    assert final_status.get("state") == "stopped"
                    observed = {entry.scenario_id: collector.called_tools for entry in TOOL_ACCEPTANCE if entry.subsystem == "clangd"}
                    validate_manifest(
                        clangd_tools, registered_scenarios=observed, observed_calls=observed,
                        available_capabilities=frozenset({"mcp_stdio", "clangd", "compile_commands"}),
                        subsystems=frozenset({"clangd"}),
                    )
        return errors.read_text(encoding="utf-8")

    server_errors = asyncio.run(exercise())
    assert '"event": "application_stopped"' in server_errors
    assert str(root) not in server_errors
    assert _tree_hashes(FIXTURE_ROOT) == committed_before
