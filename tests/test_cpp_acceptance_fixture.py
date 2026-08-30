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
from forgemcp.core.config import ForgeConfig
from forgemcp.toolchain import ToolchainDiscoveryService


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


def test_acceptance_collector_rejects_handler_or_service_call_tool_lookalikes() -> None:
    class _DirectHandlerLookalike:
        async def call_tool(self, tool_name: str, arguments: object) -> object:
            raise AssertionError("a direct handler must never be invoked")

    async def exercise() -> None:
        collector = McpToolCallCollector("core_fixture")
        with pytest.raises(AssertionError, match="official SDK ClientSession"):
            await collector.call(_DirectHandlerLookalike(), "server_status")
        assert collector.called_tools == set()

    asyncio.run(exercise())


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
    assert "FIXTURE_STEP_OVER_MARKER" in (FIXTURE_ROOT / "debug/debug_main.cpp").read_text(encoding="utf-8")
    assert "FIXTURE_STEP_IN_MARKER" in (FIXTURE_ROOT / "debug/debug_main.cpp").read_text(encoding="utf-8")
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


@pytest.mark.skipif(not _portable_prerequisites(), reason="capability_absent: real build-tree adoption requires CMake and Ninja")
def test_cpp_acceptance_fixture_adopts_an_external_build_tree_over_mcp(tmp_path: Path) -> None:
    """A pre-existing, File-API-ready tree is read before ForgeMCP uses it.

    The setup models an IDE-owned configuration: no ForgeMCP application is
    running while CMake creates the cache, database, and File API reply.
    """
    committed_before = _tree_hashes(FIXTURE_ROOT)
    root = _copy_fixture(tmp_path)
    discovery = ToolchainDiscoveryService(ForgeConfig(workspace_root=root))
    candidates = [
        discovery.kit_profile(kit.id) for kit in discovery.kits().kits
        if kit.readiness == "ready" and kit.origin == "standalone" and kit.compiler_family == "clang"
    ]
    profile = next((item for item in candidates if item is not None), None)
    if profile is None or profile.c_compiler_path is None or profile.cxx_compiler_path is None:
        pytest.skip("capability_absent: production discovery found no ready standalone Clang kit")
    build = root / "build-adopted"
    query = build / ".cmake" / "api" / "v1" / "query" / "codemodel-v2"
    query.parent.mkdir(parents=True)
    query.write_text("", encoding="utf-8")
    environment = dict(os.environ)
    if profile.environment is not None:
        environment.update(profile.environment)
    configured = subprocess.run(
        [
            str(discovery.executable("cmake")), "-S", str(root), "-B", str(build), "-G", "Ninja",
            f"-DCMAKE_C_COMPILER:FILEPATH={profile.c_compiler_path}",
            f"-DCMAKE_CXX_COMPILER:FILEPATH={profile.cxx_compiler_path}",
            "-DCMAKE_BUILD_TYPE=Debug", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        ],
        cwd=root, env=environment, text=True, capture_output=True, timeout=120, check=False,
    )
    assert configured.returncode == 0
    # Explicit CRLF is part of the cache-reader interoperability contract.
    cache = build / "CMakeCache.txt"
    cache.write_bytes(cache.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert (build / "compile_commands.json").is_file()
    assert any((build / ".cmake" / "api" / "v1" / "reply").glob("index-*.json"))
    before_read_only = _tree_hashes(build)

    async def exercise() -> None:
        parameters = StdioServerParameters(
            command=sys.executable, args=["-m", "forgemcp.server"], cwd=Path.cwd(),
            env={
                **os.environ,
                "FORGEMCP_WORKSPACE": str(root),
                "FORGEMCP_LOG_LEVEL": "INFO",
                # Bind discovery to the exact compiler pair that created the
                # external tree; family-only adoption is intentionally denied.
                "FORGEMCP_CMAKE_KIT": profile.kit.id,
            },
        )
        async with stdio_client(parameters) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                listed = _json(await session.call_tool("cmake__list_build_trees", {}))
                trees = listed["build_trees"]
                adopted = next(item for item in trees if item["binary_dir"] == "build-adopted")
                assert adopted["category"] == "adoptable"
                assert adopted["source_matches_workspace"] is True
                assert adopted["generator"] == "Ninja"
                assert adopted["compilation_database"]["availability"] == "available"
                # list/adoption discovery is observably read-only: no configure
                # call has been made and all generated metadata is unchanged.
                assert _tree_hashes(build) == before_read_only
                kits = _json(await session.call_tool("cmake__list_kits", {}))["kits"]
                selected = next(item for item in kits if item["id"] == profile.kit.id)
                assert _json(await session.call_tool("cmake__select_kit", {
                    "kit": selected["id"], "expected_selection_generation": 0,
                }))["selection_generation"] == 1
                targets = _json(await session.call_tool("cmake__list_targets", {"binary_dir": "build-adopted"}))
                assert any(target["name"] == "fixture_good" for config in targets["configurations"] for target in config["targets"])
                built = _json(await session.call_tool("cmake__build", {"binary_dir": "build-adopted"}))
                assert built["process"]["exit_code"] == 0
                tests = _json(await session.call_tool("cmake__ctest_list_tests", {"binary_dir": "build-adopted"}))
                assert {item["name"] for item in tests["tests"]} >= {"fixture_pass", "fixture_expected_failure"}
                ran = _json(await session.call_tool("cmake__ctest_run", {"binary_dir": "build-adopted"}))
                assert ran["process"]["exit_code"] == 0
                status = _json(await session.call_tool("project__status", {}))
                assert any(item["id"] == "cmake" for item in status["components"])

    asyncio.run(exercise())
    assert _tree_hashes(FIXTURE_ROOT) == committed_before


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
                    collector = McpToolCallCollector(("core_fixture", "cmake_fixture", "quality_fixture"))
                    visited: set[str] = set()
                    progress: dict[str, list[tuple[float, float | None, str | None]]] = {}

                    async def call(
                        name: str,
                        arguments: dict[str, object] | None = None,
                        **kwargs: object,
                    ) -> dict[str, object]:
                        visited.add(name)
                        return _json(await collector.call(session, name, arguments or {}, **kwargs))

                    def observe(key: str):
                        progress[key] = []

                        async def callback(value: float, total: float | None, message: str | None) -> None:
                            progress[key].append((value, total, message))

                        return callback

                    server_status = await call("server_status")
                    assert server_status["workspace_root"] == "configured"
                    project_status = await call("project__status")
                    assert project_status["partial"] is False and len(project_status["components"]) == 8
                    listed_files = await call("workspace__list_files", {"path": ".", "recursive": True})
                    assert "CMakeLists.txt" in {item["path"] for item in listed_files["files"]}
                    before = await call("workspace__read_text", {"path": "analysis/format_me.cpp"})
                    assert "int" in before["text"] and before["path"] == "analysis/format_me.cpp"
                    snapshot = before["snapshot"]["sha256"]  # type: ignore[index]
                    snap = await call("workspace__get_snapshot", {"path": "analysis/format_me.cpp"})
                    assert snap["snapshot"]["sha256"] == snapshot and snap["snapshot"]["exists"] is True  # type: ignore[index]
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

                    cmake_status = await call("cmake__status")
                    assert cmake_status["available"] is True
                    kits = await call("cmake__list_kits")
                    ready_kits = [item for item in kits["kits"] if item["readiness"] == "ready"]  # type: ignore[index]
                    assert ready_kits
                    # The portable live flow uses the standalone LLVM kit.
                    # MSVC/Developer-environment behaviour remains covered by
                    # its dedicated Windows gate rather than conflating the
                    # two qualified workflows.
                    preferred_kit = next((item for item in ready_kits if item["compiler_family"] == "clang"), ready_kits[0])
                    selected = await call("cmake__select_kit", {
                        "kit": preferred_kit["id"], "expected_selection_generation": 0,
                    })
                    assert selected["selection_generation"] == 1
                    trees_before = await call("cmake__list_build_trees")
                    assert isinstance(trees_before["build_trees"], list)
                    presets = await call("cmake__list_presets")
                    assert "ninja-debug" in {item["name"] for item in presets["configure_presets"]}  # type: ignore[index]
                    configured = await call("cmake__configure", {
                        "binary_dir": "build", "generator": "Ninja",
                        "cache_variables": {"CMAKE_BUILD_TYPE": "Debug"},
                    }, progress_callback=observe("configure"))
                    assert configured["process"]["exit_code"] == 0  # type: ignore[index]
                    assert configured["compilation_database"]["availability"] == "available"  # type: ignore[index]
                    targets = await call("cmake__list_targets", {"binary_dir": "build"})
                    target_names = {
                        target["name"] for configuration in targets["configurations"]  # type: ignore[index]
                        for target in configuration["targets"]
                    }
                    assert {"fixture_good", "fixture_warning", "fixture_compile_error", "fixture_link_error"} <= target_names
                    warning = await call(
                        "cmake__build", {"binary_dir": "build", "targets": ["fixture_warning"]},
                        progress_callback=observe("warning"),
                    )
                    assert warning["process"]["exit_code"] == 0  # type: ignore[index]
                    assert warning["outcome"] in {"success", "success_with_warnings"}
                    built = await call("cmake__build", {"binary_dir": "build"}, progress_callback=observe("build"))
                    assert built["process"]["exit_code"] == 0, json.dumps(built["process"], indent=2)  # type: ignore[index]
                    assert built["outcome"] == "success"
                    compile_failure = await call(
                        "cmake__build", {"binary_dir": "build", "targets": ["fixture_compile_error"]},
                        progress_callback=observe("compile_failure"),
                    )
                    assert compile_failure["process"]["exit_code"] != 0  # type: ignore[index]
                    assert compile_failure["diagnostics"]  # type: ignore[index]
                    assert compile_failure["outcome"] == "compile_failure"
                    link_failure = await call(
                        "cmake__build", {"binary_dir": "build", "targets": ["fixture_link_error"]},
                        progress_callback=observe("link_failure"),
                    )
                    assert link_failure["process"]["exit_code"] != 0  # type: ignore[index]
                    assert link_failure["outcome"] == "linker_failure"
                    tests = await call("cmake__ctest_list_tests", {"binary_dir": "build"})
                    assert {item["name"] for item in tests["tests"]} == {"fixture_pass", "fixture_expected_failure"}  # type: ignore[index]
                    passed = await call("cmake__ctest_run", {"binary_dir": "build"}, progress_callback=observe("test_success"))
                    assert passed["process"]["exit_code"] == 0  # type: ignore[index]
                    assert passed["failed_tests"] == []
                    negative_configure = await call("cmake__configure", {
                        "binary_dir": "build-negative", "generator": "Ninja",
                        "cache_variables": {"CMAKE_BUILD_TYPE": "Debug", "FIXTURE_ENABLE_NEGATIVE_TESTS": True},
                    })
                    assert negative_configure["process"]["exit_code"] == 0  # type: ignore[index]
                    negative_build = await call("cmake__build", {"binary_dir": "build-negative", "targets": ["fixture_tests"]})
                    assert negative_build["process"]["exit_code"] == 0  # type: ignore[index]
                    negative_tests = await call("cmake__ctest_list_tests", {"binary_dir": "build-negative"})
                    assert {item["name"] for item in negative_tests["tests"]} >= {"fixture_intentional_failure", "fixture_timeout"}  # type: ignore[index]
                    failing_test = await call(
                        "cmake__ctest_run", {"binary_dir": "build-negative", "test_names": ["fixture_intentional_failure"]},
                        progress_callback=observe("test_failure"),
                    )
                    assert failing_test["test_names"] == ["fixture_intentional_failure"]
                    assert failing_test["process"]["exit_code"] != 0 and failing_test["failed_tests"] == ["fixture_intentional_failure"]  # type: ignore[index]
                    timeout_test = await call(
                        "cmake__ctest_run", {"binary_dir": "build-negative", "test_names": ["fixture_timeout"], "timeout_seconds": 3},
                        progress_callback=observe("test_timeout"),
                    )
                    assert timeout_test["test_names"] == ["fixture_timeout"]
                    assert timeout_test["process"]["exit_code"] != 0 and timeout_test["failed_tests"] == ["fixture_timeout"]  # type: ignore[index]
                    conflict = await call("cmake__configure", {"binary_dir": "build-conflict", "preset": "ninja-debug"})
                    assert conflict["error"]["code"] == "preset_kit_conflict"  # type: ignore[index]

                    quality = await call("quality__status")
                    assert quality["clang_format"]["available"] is True and quality["clang_format"]["executable"] == "clang-format"  # type: ignore[index]
                    assert quality["clang_tidy"]["available"] is True and quality["clang_tidy"]["executable"] == "clang-tidy"  # type: ignore[index]
                    checked = await call("clang_format__check", {"paths": ["analysis/format_me.cpp"]})
                    item = checked["files"][0]  # type: ignore[index]
                    assert item["would_change"] is True
                    applied = await call("clang_format__apply", {"files": [{"path": item["path"], "expected_sha256": item["snapshot_sha256"]}]})
                    assert applied["applied"] is True
                    tidy_checks = await call("clang_tidy__list_checks", {"checks": "modernize-use-nullptr"})
                    assert "modernize-use-nullptr" in tidy_checks["checks"]
                    tidy = await call("clang_tidy__run", {"paths": ["analysis/tidy_me.cpp"], "compile_commands_dir": "build", "checks": "-*,modernize-use-nullptr"})
                    assert tidy["execution_state"] == "completed"
                    assert any(item.get("code") == "modernize-use-nullptr" for item in tidy["diagnostics"])
                    report = (root / "reports" / "asan.txt").read_text(encoding="utf-8")
                    sanitizer = await call("sanitizer__parse_report", {"output": report})
                    assert sanitizer["findings"][0]["kind"] == "address_sanitizer"  # type: ignore[index]
                    for key, updates in progress.items():
                        assert updates, key
                        values = [value for value, _, _ in updates]
                        assert values == sorted(values), (key, updates)
                    for key in ("configure", "warning", "build", "test_success"):
                        assert progress[key][-1][2] in {"Configure completed", "Build completed", "Test run completed"}
                    for key in ("compile_failure", "link_failure", "test_failure", "test_timeout"):
                        updates = progress[key]
                        assert updates[-1][2] in {"Build failed", "Test run failed", "Test run timed out"}
                        assert not any(total is not None and value == total for value, total, _ in updates)
                    mandatory = {
                        entry.tool_name for entry in TOOL_ACCEPTANCE
                        if entry.subsystem in {"core", "cmake", "quality"}
                    }
                    assert mandatory <= visited
                    observed = {
                        entry.scenario_id: collector.called_tools
                        for entry in TOOL_ACCEPTANCE
                        if entry.subsystem in {"core", "cmake", "quality"}
                    }
                    validate_manifest(
                        mandatory, registered_scenarios=observed, observed_calls=observed,
                        available_capabilities=frozenset({"mcp_stdio", "cmake", "ninja"}),
                        subsystems=frozenset({"core", "cmake", "quality"}),
                    )
                    collector.complete_assertions(mandatory)
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
                    assert isinstance(rename_edit, dict) and rename_edit.get("applied") is True, renamed
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
                    collector.complete_assertions(clangd_tools)
        return errors.read_text(encoding="utf-8")

    server_errors = asyncio.run(exercise())
    assert '"event": "application_stopped"' in server_errors
    assert str(root) not in server_errors
    assert _tree_hashes(FIXTURE_ROOT) == committed_before


@pytest.mark.debugger_fixture_mcp
@pytest.mark.skipif(not _portable_prerequisites(), reason="debugger_fixture_unavailable: CMake or Ninja is unavailable")
def test_cpp_acceptance_fixture_real_debugger_all_tools_mcp_gate(tmp_path: Path) -> None:
    """Exercise every published debugger tool over real MCP stdio and LLDB-DAP.

    The gate deliberately uses no debugger-service calls or compiler command
    outside MCP.  Discovery is production-equivalent and the only permissible
    skip is a single capability summary before a qualified chain exists.
    """
    committed_before = _tree_hashes(FIXTURE_ROOT)
    root = _copy_fixture(tmp_path)

    async def exercise() -> str:
        errors = tmp_path / "debugger-server-stderr.log"
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
                    debugger_tools = {tool.name for tool in listed.tools if tool.name.startswith("debugger__")}
                    manifest_tools = {entry.tool_name for entry in TOOL_ACCEPTANCE if entry.subsystem == "debugger"}
                    assert debugger_tools == manifest_tools
                    assert len(debugger_tools) == 16
                    assert "debugger__step_over" in debugger_tools  # Public name for DAP `next`.

                    collector = McpToolCallCollector("debugger_fixture")

                    async def call(name: str, arguments: dict[str, object] | None = None) -> dict[str, object]:
                        result = await collector.call(session, name, arguments)
                        payload = _json(result)
                        assert getattr(result, "isError") is False and "error" not in payload, (name, payload)
                        return payload

                    async def expect_error(name: str, arguments: dict[str, object]) -> dict[str, object]:
                        result = await collector.call(session, name, arguments)
                        payload = _json(result)
                        assert getattr(result, "isError") or "error" in payload, (name, payload)
                        return payload

                    async def wait_for_state(*states: str, timeout: float = 12.0) -> dict[str, object]:
                        deadline = asyncio.get_running_loop().time() + timeout
                        while True:
                            status = await call("debugger__status")
                            if status.get("state") in states:
                                return status
                            if asyncio.get_running_loop().time() >= deadline:
                                raise AssertionError(f"debugger did not reach {states}: {status}")
                            await asyncio.sleep(0.05)

                    def current_thread(payload: dict[str, object]) -> str:
                        threads = payload.get("threads")
                        assert isinstance(threads, list) and threads
                        current = next((item for item in threads if isinstance(item, dict) and item.get("is_current") is True), None)
                        assert isinstance(current, dict), threads
                        token = current.get("thread_id")
                        assert isinstance(token, str)
                        return token

                    initial = await call("debugger__status")
                    adapters = await call("debugger__list_adapters")
                    adapter_values = adapters.get("adapters")
                    assert isinstance(adapter_values, list) and len(adapter_values) == 1
                    adapter = adapter_values[0]
                    assert isinstance(adapter, dict)
                    kits = await call("cmake__list_kits")
                    kit_values = kits.get("kits")
                    assert isinstance(kit_values, list)
                    standalone = next((kit for kit in kit_values if isinstance(kit, dict)
                        and kit.get("readiness") == "ready" and kit.get("origin") == "standalone"
                        and kit.get("compiler_family") == "clang" and kit.get("driver_mode") == "clang++"
                        and kit.get("abi") == "llvm" and kit.get("debugger_compatibility") == "compatible"), None)
                    if not adapter.get("available") or standalone is None:
                        reason = "debugger_fixture_unavailable: no qualified standalone LLVM/Clang/DWARF/lldb-dap chain"
                        assert initial.get("state") in {"stopped", "unavailable"}
                        pytest.skip(reason)
                    assert adapter.get("backend_id") == "lldb-dap"
                    assert adapter.get("source") == "standalone"
                    assert "path" not in json.dumps(adapter).lower()
                    assert "\\\\" not in json.dumps(adapter)
                    kit_id = standalone.get("id")
                    assert isinstance(kit_id, str)
                    selected = await call("cmake__select_kit", {"kit": kit_id, "expected_selection_generation": 0})
                    assert selected.get("selection_generation") == 1
                    configured = await call("cmake__configure", {
                        "binary_dir": "build-debugger", "generator": "Ninja",
                        "cache_variables": {"CMAKE_BUILD_TYPE": "Debug", "FIXTURE_LLVM_DWARF": True},
                    })
                    assert configured.get("process", {}).get("exit_code") == 0
                    assert configured.get("effective_kit", {}).get("id") == kit_id
                    built = await call("cmake__build", {"binary_dir": "build-debugger", "targets": ["fixture_debug"]})
                    assert built.get("process", {}).get("exit_code") == 0
                    targets = await call("cmake__list_targets", {"binary_dir": "build-debugger"})
                    fixture_target = next(
                        target for configuration in targets.get("configurations", []) if isinstance(configuration, dict)
                        for target in configuration.get("targets", []) if isinstance(target, dict) and target.get("name") == "fixture_debug"
                    )
                    artifacts = fixture_target.get("artifacts")
                    assert isinstance(artifacts, list)
                    program = next(item for item in artifacts if isinstance(item, str) and item.endswith(".exe"))
                    assert isinstance(program, str) and program.endswith(".exe") and not Path(program).is_absolute()
                    debug_text = (root / "debug/debug_main.cpp").read_text(encoding="utf-8")
                    breakpoint_line = _position(debug_text, "FIXTURE_STEP_OVER_MARKER", offset=len("FIXTURE_STEP_OVER_MARKER") + 1)["line"]

                    # Session one: entry stop -> verified breakpoint -> inspection -> steps -> terminal.
                    launched = await call("debugger__launch", {"program": program, "cwd": "build-debugger", "stop_on_entry": True})
                    assert launched.get("state") in {"initialized", "configuring", "running", "paused"}
                    await wait_for_state("paused")
                    breakpoints = await call("debugger__set_breakpoints", {
                        "path": "debug/debug_main.cpp", "breakpoints": [{"line": breakpoint_line}],
                    })
                    values = breakpoints.get("breakpoints")
                    assert isinstance(values, list) and values and values[0].get("verified") is True
                    entry_threads = await call("debugger__threads")
                    entry_thread = current_thread(entry_threads)
                    await call("debugger__continue", {"thread_id": entry_thread})
                    paused = await wait_for_state("paused")
                    assert paused.get("stop_generation", 0) >= 2
                    paused_threads = await call("debugger__threads")
                    thread_id = current_thread(paused_threads)
                    frames_payload = await call("debugger__stack_trace", {"thread_id": thread_id})
                    frames = frames_payload.get("frames")
                    assert isinstance(frames, list) and frames
                    frame = next((item for item in frames if isinstance(item, dict) and item.get("source", {}).get("path") == "debug/debug_main.cpp"), None)
                    assert isinstance(frame, dict), [(item.get("name"), item.get("source"), item.get("line")) for item in frames if isinstance(item, dict)]
                    frame_id = frame["frame_id"]
                    assert isinstance(frame_id, str)
                    scopes_payload = await call("debugger__scopes", {"frame_id": frame_id})
                    scopes = scopes_payload.get("scopes")
                    assert isinstance(scopes, list) and scopes
                    variables_id = next(item["variables_id"] for item in scopes if isinstance(item, dict) and item.get("variables_id"))
                    variables = await call("debugger__variables", {"variables_id": variables_id})
                    assert any(item.get("name") == "seed" and "40" in item.get("value", "") for item in variables.get("variables", []) if isinstance(item, dict))
                    evaluated = await call("debugger__evaluate", {"frame_id": frame_id, "expression": "seed"})
                    assert "40" in str(evaluated.get("result")) and evaluated.get("side_effects_possible") is True
                    for expression in ("seed.member", "seed[0]", "*seed", "call()", "seed + 1", " seed", "seéd"):
                        result = await session.call_tool("debugger__evaluate", {"frame_id": frame_id, "expression": expression})
                        rejected = _json(result)
                        assert getattr(result, "isError") or "error" in rejected, (expression, rejected)
                        assert rejected.get("error", {}).get("code") in {"debugger_unsupported", "debugger_request_error"}
                    rejected_extra = await session.call_tool("debugger__evaluate", {"frame_id": frame_id, "expression": "seed", "unexpected": True})
                    assert getattr(rejected_extra, "isError") is True

                    before_line = frame.get("line")
                    await call("debugger__step_over", {"thread_id": thread_id})
                    await wait_for_state("paused")
                    stale = await expect_error("debugger__stack_trace", {"thread_id": thread_id})
                    assert stale.get("error", {}).get("code") in {"debugger_handle_expired", "debugger_stopped_data_stale"}
                    step_threads = await call("debugger__threads")
                    step_thread = current_thread(step_threads)
                    step_frames = await call("debugger__stack_trace", {"thread_id": step_thread})
                    step_frame = step_frames["frames"][0]
                    assert step_frame.get("line") != before_line
                    await call("debugger__step_in", {"thread_id": step_thread})
                    await wait_for_state("paused")
                    in_thread = current_thread(await call("debugger__threads"))
                    in_frames = (await call("debugger__stack_trace", {"thread_id": in_thread}))["frames"]
                    assert any("debug_middle" in str(item.get("name")) for item in in_frames if isinstance(item, dict)), [(item.get("name"), item.get("source"), item.get("line")) for item in in_frames if isinstance(item, dict)]
                    await call("debugger__step_out", {"thread_id": in_thread})
                    await wait_for_state("paused")
                    out_thread = current_thread(await call("debugger__threads"))
                    out_frames = (await call("debugger__stack_trace", {"thread_id": out_thread}))["frames"]
                    assert any(str(item.get("name", "")).startswith("main") for item in out_frames if isinstance(item, dict))
                    await call("debugger__continue", {"thread_id": out_thread})
                    terminal = await wait_for_state("terminated", "stopped")
                    assert terminal.get("state") == "terminated"
                    events = await call("debugger__events", {"limit": 256})
                    event_values = events.get("events")
                    assert isinstance(event_values, list) and event_values
                    sequences = [event["sequence"] for event in event_values if isinstance(event, dict)]
                    assert sequences == sorted(sequences) and any(event.get("kind") == "stopped" for event in event_values if isinstance(event, dict))
                    assert any(event.get("kind") == "terminated" for event in event_values if isinstance(event, dict))
                    first_terminal_cursor = events.get("next_cursor")
                    stopped = await call("debugger__stop")
                    assert stopped.get("state") == "terminated" and stopped.get("debuggee_termination_confirmed") is True

                    # Session two: prove a real RUNNING -> PAUSED pause flow and session-bound stale handles.
                    running = await call("debugger__launch", {"program": program, "cwd": "build-debugger", "args": ["bounded-running"], "stop_on_entry": False})
                    assert running.get("state") in {"running", "paused"}
                    await wait_for_state("running")
                    old_session = await expect_error("debugger__stack_trace", {"thread_id": entry_thread})
                    assert old_session.get("error", {}).get("code") == "debugger_invalid_state"
                    await call("debugger__pause")
                    await wait_for_state("paused")
                    stale_session = await expect_error("debugger__stack_trace", {"thread_id": entry_thread})
                    assert stale_session.get("error", {}).get("code") == "debugger_handle_expired"
                    pause_threads = await call("debugger__threads")
                    pause_thread = current_thread(pause_threads)
                    assert (await call("debugger__stack_trace", {"thread_id": pause_thread}))["frames"]
                    paused_stop = await call("debugger__stop")
                    assert paused_stop.get("state") == "terminated"
                    retained = await call("debugger__events", {"after_sequence": 0, "limit": 256})
                    assert retained.get("next_cursor", 0) >= 1 and retained.get("next_cursor") != first_terminal_cursor

                    # Session three: stop a genuinely running debuggee before natural completion.
                    await call("debugger__launch", {"program": program, "cwd": "build-debugger", "args": ["bounded-running"], "stop_on_entry": False})
                    await wait_for_state("running")
                    running_stop = await call("debugger__stop")
                    assert running_stop.get("state") == "terminated" and running_stop.get("debuggee_termination_confirmed") is True
                    assert (await call("debugger__stop")).get("state") == "terminated"

                    observed = {entry.scenario_id: collector.called_tools for entry in TOOL_ACCEPTANCE if entry.subsystem == "debugger"}
                    validate_manifest(
                        debugger_tools, registered_scenarios=observed, observed_calls=observed,
                        available_capabilities=frozenset({"mcp_stdio", "standalone_llvm", "lldb_dap", "dwarf_debuggee"}),
                        subsystems=frozenset({"debugger"}),
                    )
                    collector.complete_assertions(debugger_tools)
        return errors.read_text(encoding="utf-8")

    server_errors = asyncio.run(exercise())
    assert '"event": "application_stopped"' in server_errors
    assert str(root) not in server_errors
    assert _tree_hashes(FIXTURE_ROOT) == committed_before
