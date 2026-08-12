"""Unit and optional integration coverage for the builtin CMake feature plugin."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgemcp.cmake import (
    CMakeFileApiError,
    CMakePresetError,
    CMakeRequestError,
    CMakeService,
    CTestJsonError,
)
from forgemcp.core.application import ForgeApplication
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.models import ProcessOutput, ProcessResult
from forgemcp.processes import ProcessExecutableError, ProcessRuntime
from forgemcp.server import create_server
from forgemcp.workspace import SymlinkWorkspacePathError, WorkspacePathError, WorkspaceService


FIXTURES = Path(__file__).parent / "fixtures" / "cmake_file_api"


def process_result(*, exit_code: int = 0, stdout: str = "", stderr: str = "") -> ProcessResult:
    now = datetime.now(UTC)
    return ProcessResult(
        exit_code=exit_code,
        started_at=now,
        finished_at=now,
        stdout=ProcessOutput(text=stdout),
        stderr=ProcessOutput(text=stderr),
    )


class FakeProcessRuntime:
    """A command-recording ProcessRuntime substitute with deterministic results."""

    def __init__(self, responses: Sequence[ProcessResult | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], str, float | None]] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        environment=None,
        inherit_environment=None,
        timeout_seconds: float | None = None,
    ) -> ProcessResult:
        self.calls.append((tuple(argv), cwd, timeout_seconds))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def cmake_service(root: Path, responses: Sequence[ProcessResult | Exception]) -> tuple[CMakeService, FakeProcessRuntime]:
    workspace = WorkspaceService(ForgeConfig(workspace_root=root), create_logger("CRITICAL"))
    runtime = FakeProcessRuntime(responses)
    return CMakeService(workspace, runtime), runtime


def prepare_project(root: Path, *, build: str = "build") -> None:
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (root / build).mkdir(exist_ok=True)


def install_file_api(root: Path, *, multi_config: bool = False) -> None:
    workspace = WorkspaceService(ForgeConfig(workspace_root=root), create_logger("CRITICAL"))
    generated = workspace.open_generated_directory("build", create=True)
    index = json.loads((FIXTURES / "index-v2.json").read_text(encoding="utf-8"))
    codemodel = "codemodel-multi-v2.json" if multi_config else "codemodel-v2.json"
    index["reply"]["codemodel-v2"]["jsonFile"] = codemodel
    generated.write_text(".cmake/api/v1/reply/index-test.json", json.dumps(index))
    filenames = [codemodel]
    filenames.extend(["target-debug.json", "target-release.json"] if multi_config else ["target-app.json", "target-lib.json"])
    for filename in filenames:
        generated.write_text(
            f".cmake/api/v1/reply/{filename}",
            (FIXTURES / filename).read_text(encoding="utf-8"),
        )


def test_status_parses_versions_checks_minimum_and_explains_missing_tools(tmp_path):
    service, runtime = cmake_service(
        tmp_path,
        [process_result(stdout="cmake version 3.28.4\n"), ProcessExecutableError("missing")],
    )

    status = asyncio.run(service.status())

    assert status.available is False
    assert status.cmake.version is not None and status.cmake.version.full == "3.28.4"
    assert status.cmake.supported is True
    assert status.ctest.available is False
    assert status.ctest.error is not None and "ctest" in status.ctest.error
    assert [call[0] for call in runtime.calls] == [("cmake", "--version"), ("ctest", "--version")]


def test_status_marks_old_cmake_unsupported(tmp_path):
    service, _ = cmake_service(
        tmp_path,
        [process_result(stdout="cmake version 3.18.2\n"), process_result(stdout="ctest version 3.18.2\n")],
    )

    status = asyncio.run(service.status())

    assert status.available is False
    assert status.cmake.supported is False
    assert status.cmake.error is not None and "supported minimum" in status.cmake.error


def test_list_presets_omits_environment_and_cache_secrets_and_handles_absence(tmp_path):
    project = {
        "version": 4,
        "configurePresets": [
            {
                "name": "debug",
                "displayName": "Debug",
                "generator": "Ninja",
                "environment": {"TOKEN": "do-not-return"},
                "cacheVariables": {"API_KEY": "do-not-return"},
            }
        ],
        "buildPresets": [{"name": "debug-build", "configurePreset": "debug", "targets": ["app"]}],
        "testPresets": [{"name": "debug-test", "configurePreset": "debug"}],
    }
    (tmp_path / "CMakePresets.json").write_text(json.dumps(project), encoding="utf-8")
    service, _ = cmake_service(tmp_path, [])

    presets = asyncio.run(service.list_presets())

    assert presets.preset_files == ("CMakePresets.json",)
    assert presets.configure_presets[0].generator == "Ninja"
    assert presets.build_presets[0].targets == ("app",)
    assert presets.test_presets[0].name == "debug-test"
    assert "TOKEN" not in presets.model_dump_json()
    assert "API_KEY" not in presets.model_dump_json()


def test_list_presets_absent_and_malformed(tmp_path):
    (tmp_path / "empty").mkdir()
    service, _ = cmake_service(tmp_path, [])

    absent = asyncio.run(service.list_presets(source_dir="empty"))

    assert absent.preset_files == ()
    (tmp_path / "CMakeUserPresets.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(CMakePresetError):
        asyncio.run(service.list_presets())


def test_configure_writes_file_api_query_uses_argv_and_validated_cache_variables(tmp_path):
    prepare_project(tmp_path)
    service, runtime = cmake_service(tmp_path, [process_result(stdout="configured")])

    result = asyncio.run(
        service.configure(
            source_dir="src",
            binary_dir="build",
            preset="debug",
            cache_variables={"FEATURE": True, "COUNT": 2, "NAME": "safe"},
        )
    )

    assert result.process.exit_code == 0
    assert runtime.calls == [
        (
            (
                "cmake",
                "-S",
                "src",
                "-B",
                "build",
                "--preset",
                "debug",
                "-DFEATURE:STRING=ON",
                "-DCOUNT:STRING=2",
                "-DNAME:STRING=safe",
            ),
            ".",
            None,
        )
    ]
    assert (tmp_path / "build" / ".cmake" / "api" / "v1" / "query" / "codemodel-v2").exists()

    with pytest.raises(CMakeRequestError):
        asyncio.run(service.configure(source_dir="src", binary_dir="build", cache_variables={"bad-key": "x"}))


def test_configure_rejects_workspace_escape_and_symlink_build_directory(tmp_path):
    prepare_project(tmp_path)
    service, _ = cmake_service(tmp_path, [process_result()])

    with pytest.raises(WorkspacePathError):
        asyncio.run(service.configure(source_dir="src", binary_dir="../outside"))

    outside = tmp_path.parent / "cmake-build-outside"
    outside.mkdir()
    link = tmp_path / "build-link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Symbolic links are unavailable in this test environment: {error}")
    with pytest.raises(SymlinkWorkspacePathError):
        asyncio.run(service.configure(source_dir="src", binary_dir="build-link"))


def test_file_api_codemodel_v2_lists_single_and_multi_configuration_targets(tmp_path):
    prepare_project(tmp_path)
    install_file_api(tmp_path)
    service, _ = cmake_service(tmp_path, [])

    single = service.list_targets(binary_dir="build")

    assert single.configurations[0].name == ""
    app = single.configurations[0].targets[0]
    assert app.name == "app"
    assert app.type == "EXECUTABLE"
    assert app.artifacts == ("build/app.exe",)
    assert app.sources == ("src/main.cpp",)
    assert app.dependencies == ("lib",)

    (tmp_path / "build" / "Debug").mkdir()
    (tmp_path / "build" / "Release").mkdir()
    install_file_api(tmp_path, multi_config=True)
    multi = service.list_targets(binary_dir="build")

    assert [configuration.name for configuration in multi.configurations] == ["Debug", "Release"]
    assert [target.build_directory for configuration in multi.configurations for target in configuration.targets] == [
        "build/Debug",
        "build/Release",
    ]


def test_file_api_rejects_missing_stale_malformed_and_outside_paths(tmp_path):
    prepare_project(tmp_path)
    service, _ = cmake_service(tmp_path, [])

    with pytest.raises(CMakeFileApiError, match="reply directory is missing"):
        service.list_targets(binary_dir="build")

    workspace = WorkspaceService(ForgeConfig(workspace_root=tmp_path), create_logger("CRITICAL"))
    generated = workspace.open_generated_directory("build")
    generated.write_text(".cmake/api/v1/reply/index-stale.json", '{"reply": {}}')
    with pytest.raises(CMakeFileApiError, match="stale"):
        service.list_targets(binary_dir="build")

    generated.write_text(
        ".cmake/api/v1/reply/index-stale.json",
        '{"reply": {"codemodel-v2": {"jsonFile": "broken.json"}}}',
    )
    generated.write_text(".cmake/api/v1/reply/broken.json", "not-json")
    with pytest.raises(CMakeFileApiError, match="malformed"):
        service.list_targets(binary_dir="build")

    generated.write_text(".cmake/api/v1/reply/index-stale.json", json.dumps(json.loads((FIXTURES / "index-v2.json").read_text(encoding="utf-8"))))
    generated.write_text(".cmake/api/v1/query/codemodel-v2", "new-query")
    with pytest.raises(CMakeFileApiError, match="predates"):
        service.list_targets(binary_dir="build")

    index = json.loads((FIXTURES / "index-v2.json").read_text(encoding="utf-8"))
    generated.write_text(".cmake/api/v1/reply/index-stale.json", json.dumps(index))
    install_file_api(tmp_path)
    outside = tmp_path.parent / "untrusted-file-api-source"
    outside.mkdir()
    bad_codemodel = json.loads((FIXTURES / "codemodel-v2.json").read_text(encoding="utf-8"))
    bad_codemodel["paths"]["source"] = str(outside)
    generated.write_text(".cmake/api/v1/reply/codemodel-v2.json", json.dumps(bad_codemodel))
    with pytest.raises(CMakeFileApiError, match="outside"):
        service.list_targets(binary_dir="build")


def test_build_returns_nonzero_process_result_and_enforces_parallel_bound(tmp_path):
    prepare_project(tmp_path)
    service, runtime = cmake_service(tmp_path, [process_result(exit_code=7, stderr="compile failed")])

    result = asyncio.run(
        service.build(binary_dir="build", targets=("app", "lib"), configuration="Debug", parallel_jobs=4)
    )

    assert result.process.exit_code == 7
    assert runtime.calls[0][0] == (
        "cmake",
        "--build",
        "build",
        "--target",
        "app",
        "lib",
        "--config",
        "Debug",
        "--parallel",
        "4",
    )
    with pytest.raises(CMakeRequestError):
        asyncio.run(service.build(binary_dir="build", parallel_jobs=0))


def test_ctest_json_listing_and_failed_exact_name_run(tmp_path):
    prepare_project(tmp_path)
    listing = json.dumps(
        {"kind": "ctestInfo", "version": {"major": 1, "minor": 0}, "tests": [{"name": "unit.a"}, {"name": "unit[b]"}]}
    )
    failed = "The following tests FAILED:\n  2 - unit[b] (Failed)\n"
    service, runtime = cmake_service(
        tmp_path,
        [process_result(stdout=listing), process_result(exit_code=8, stdout=failed)],
    )

    tests = asyncio.run(service.list_tests(binary_dir="build"))
    run = asyncio.run(service.run_tests(binary_dir="build", test_names=("unit.a", "unit[b]"), configuration="Debug", timeout_seconds=12.0))

    assert [test.name for test in tests.tests] == ["unit.a", "unit[b]"]
    assert run.process.exit_code == 8
    assert run.failed_tests == ("unit[b]",)
    assert runtime.calls[1] == (
        ("ctest", "--test-dir", "build", "--output-on-failure", "--build-config", "Debug", "-R", r"^(?:unit\.a|unit\[b\])$"),
        ".",
        12.0,
    )

    invalid, _ = cmake_service(tmp_path, [process_result(stdout="{}")])
    with pytest.raises(CTestJsonError):
        asyncio.run(invalid.list_tests(binary_dir="build"))


def test_builtin_plugin_lifecycle_registers_stable_tools_with_flat_input_schemas(tmp_path):
    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        server = create_server(lambda: application)
        async with server._mcp_server.lifespan(server._mcp_server):  # type: ignore[attr-defined]
            tools = {tool.name: tool for tool in await server.list_tools()}
            assert "cmake__configure" in tools
            assert set(tools["cmake__configure"].inputSchema["properties"]) == {
                "source_dir",
                "binary_dir",
                "preset",
                "cache_variables",
            }
            assert tools["cmake__configure"].inputSchema["required"] == ["binary_dir"]
            statuses = {status.plugin_id: status.state.value for status in application.services.get("plugins").statuses()}
            assert statuses["cmake"] == "running"
        statuses = {status.plugin_id: status.state.value for status in application.services.get("plugins").statuses()}
        assert statuses["cmake"] == "stopped"

    asyncio.run(exercise())


@pytest.mark.skipif(
    shutil.which("cmake") is None or shutil.which("ctest") is None or shutil.which("c++") is None,
    reason="requires CMake, CTest, and a C++ compiler on PATH",
)
def test_optional_real_cmake_vertical_slice(tmp_path):
    """Exercise the real command path only on developer machines that provide CMake and a compiler."""
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.23)\nproject(forgemcp_smoke LANGUAGES CXX)\nadd_executable(app main.cpp)\nenable_testing()\nadd_test(NAME app_runs COMMAND app)\n",
        encoding="utf-8",
    )
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    config = ForgeConfig(workspace_root=tmp_path)
    runtime = ProcessRuntime(config, create_logger("CRITICAL"))
    service = CMakeService(WorkspaceService(config, create_logger("CRITICAL")), runtime)

    async def exercise() -> None:
        configured = await service.configure(binary_dir="build")
        assert configured.process.exit_code == 0
        assert service.list_targets(binary_dir="build").configurations
        built = await service.build(binary_dir="build", targets=("app",))
        assert built.process.exit_code == 0
        tests = await service.list_tests(binary_dir="build")
        assert [test.name for test in tests.tests] == ["app_runs"]
        assert (await service.run_tests(binary_dir="build")).process.exit_code == 0
        await runtime.aclose()

    asyncio.run(exercise())
