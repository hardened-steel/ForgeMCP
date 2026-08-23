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

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.acceptance_manifest import OPTIONAL, REAL, TOOL_ACCEPTANCE


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


def test_cpp_acceptance_fixture_is_complete_and_has_no_generated_artifacts() -> None:
    required = {
        "CMakeLists.txt", "CMakePresets.json", "README.md", "include/fixture/math.hpp",
        "include/fixture/hierarchy.hpp", "src/math.cpp", "src/hierarchy.cpp",
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
            env={**os.environ, "FORGEMCP_WORKSPACE": str(root), "FORGEMCP_LOG_LEVEL": "CRITICAL"},
        )
        with errors.open("w", encoding="utf-8") as stderr:
            async with stdio_client(parameters, errlog=stderr) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    names = [tool.name for tool in (await session.list_tools()).tools]
                    assert len(names) == len(set(names))
                    assert set(names) == set(TOOL_ACCEPTANCE)
                    assert {TOOL_ACCEPTANCE[name] for name in names} <= {REAL, OPTIONAL}
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
                    assert {name for name, kind in TOOL_ACCEPTANCE.items() if kind == REAL} <= visited
        text = errors.read_text(encoding="utf-8")
        assert str(root) not in text

    asyncio.run(exercise())
