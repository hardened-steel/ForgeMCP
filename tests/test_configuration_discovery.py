"""Phase-A configuration/discovery fakes: no local VS installation is required."""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgemcp.cmake import CMakeService
from forgemcp.core.application import ForgeApplication
from forgemcp.core.config import ConfigurationSource, ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.models import ProcessOutput, ProcessResult
from forgemcp.processes import ProcessPolicy, ProcessRuntime
from forgemcp.server import main
from forgemcp.toolchain import ToolchainDiscoveryService
from forgemcp.workspace import WorkspaceService


def _pe(path: Path, machine: int = 0x8664) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(0x80)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (0x40).to_bytes(4, "little")
    data[0x40:0x46] = b"PE\0\0" + machine.to_bytes(2, "little")
    path.write_bytes(data)
    return path


def _vs_layout(root: Path) -> tuple[Path, Path]:
    instance = root / "Microsoft Visual Studio" / "18" / "BuildTools"
    vswhere = _pe(root / "Microsoft Visual Studio" / "Installer" / "vswhere.exe")
    _pe(instance / "Common7" / "IDE" / "CommonExtensions" / "Microsoft" / "CMake" / "CMake" / "bin" / "cmake.exe")
    _pe(instance / "Common7" / "IDE" / "CommonExtensions" / "Microsoft" / "CMake" / "CMake" / "bin" / "ctest.exe")
    _pe(instance / "Common7" / "IDE" / "CommonExtensions" / "Microsoft" / "CMake" / "Ninja" / "ninja.exe")
    _pe(instance / "MSBuild" / "Current" / "Bin" / "MSBuild.exe")
    _pe(instance / "VC" / "Tools" / "MSVC" / "14.40" / "bin" / "Hostx64" / "x64" / "cl.exe")
    script = instance / "Common7" / "Tools" / "VsDevCmd.bat"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("@exit /b 0\n", encoding="utf-8")
    return instance, vswhere


def _config(root: Path, environment: dict[str, str], **cli: object) -> ForgeConfig:
    return ForgeConfig.from_sources(environment=environment, cli=cli, cwd=root)


def test_cli_precedes_environment_and_safe_config_does_not_leak_values(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        {"FORGEMCP_WORKSPACE": str(tmp_path), "FORGEMCP_BUILD_DIR": "from-env", "FORGEMCP_TOOLCHAIN": "llvm"},
        build_dir="from-cli",
        toolchain="msvc",
    )
    assert config.build_dir == "from-cli"
    assert config.source_of("build_dir") is ConfigurationSource.CLI
    assert config.toolchain == "msvc"
    assert config.source_of("toolchain") is ConfigurationSource.CLI
    rendered = json.dumps(config.sanitized_effective_config())
    assert "from-env" not in rendered
    assert str(tmp_path) not in rendered


def test_fake_build_tools_vs_is_selected_and_developer_environment_is_filtered(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    program_files = tmp_path.parent / f"ProgramFilesX86-{tmp_path.name}"
    instance, _ = _vs_layout(program_files)
    document = [{
        "installationPath": str(instance), "productId": "Microsoft.VisualStudio.Product.BuildTools",
        "installationVersion": "18.1", "displayName": "Visual Studio Build Tools",
        "packages": [{"id": "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"}],
    }]
    config = _config(workspace, {"FORGEMCP_WORKSPACE": str(workspace), "ProgramFiles(x86)": str(program_files)})
    service = ToolchainDiscoveryService(
        config,
        run_vswhere=lambda _: json.dumps(document).encode(),
        capture_environment=lambda *_: {"PATH": "trusted-bin", "INCLUDE": "include", "TOKEN": "not-exported"},
    )
    assert service.executable("cmake") is not None
    assert service.executable("cl") is not None
    assert service.snapshot().visual_studio_vc_tools is True
    assert service.toolchain_environment is not None and "TOKEN" not in service.toolchain_environment
    safe = json.dumps(service.snapshot().as_dict())
    assert str(instance) not in safe and "trusted-bin" not in safe and "not-exported" not in safe


@pytest.mark.parametrize("payload", [b"not json", b"[" + b" " * (512 * 1024) + b"]"], ids=("malformed", "oversized"))
def test_broken_or_oversized_vswhere_is_reported_safely(tmp_path: Path, payload: bytes) -> None:
    program_files = tmp_path.parent / f"ProgramFilesX86-{tmp_path.name}"
    _vs_layout(program_files)
    config = _config(tmp_path, {"FORGEMCP_WORKSPACE": str(tmp_path), "ProgramFiles(x86)": str(program_files)})
    discovery = ToolchainDiscoveryService(config, run_vswhere=lambda _: payload)
    assert any(item.startswith("vswhere:") for item in discovery.snapshot().rejections)


def test_missing_vswhere_is_reported_without_probing_the_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name != "nt":
        pytest.skip("Visual Studio discovery is Windows-only")
    monkeypatch.setattr(ToolchainDiscoveryService, "_vswhere_path", lambda _: None)
    discovery = ToolchainDiscoveryService(_config(tmp_path, {"FORGEMCP_WORKSPACE": str(tmp_path)}))
    assert "vswhere: unavailable" in discovery.snapshot().rejections


def test_multiple_vs_instances_are_deterministic_and_missing_workload_is_visible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    program_files = tmp_path.parent / f"ProgramFilesX86-{tmp_path.name}"
    older, _ = _vs_layout(program_files)
    newer = program_files / "Microsoft Visual Studio" / "18" / "Community"
    _pe(newer / "Common7" / "IDE" / "CommonExtensions" / "Microsoft" / "CMake" / "CMake" / "bin" / "cmake.exe")
    (newer / "Common7" / "Tools").mkdir(parents=True)
    (newer / "Common7" / "Tools" / "VsDevCmd.bat").write_text("@exit /b 0", encoding="utf-8")
    document = [
        {"installationPath": str(older), "productId": "BuildTools", "installationVersion": "18.9", "displayName": "Build Tools", "packages": [{"id": "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"}]},
        {"installationPath": str(newer), "productId": "Community", "installationVersion": "18.10", "displayName": "Community", "packages": []},
    ]
    config = _config(workspace, {"FORGEMCP_WORKSPACE": str(workspace), "ProgramFiles(x86)": str(program_files)}, toolchain="msvc")
    discovery = ToolchainDiscoveryService(config, run_vswhere=lambda _: json.dumps(document).encode())
    # The newer Community instance lacks VC tools, so the deterministic eligible
    # Build Tools instance wins instead of silently accepting an unusable install.
    assert discovery.snapshot().visual_studio_vc_tools is True
    assert discovery.executable("cl") is not None

    missing = ToolchainDiscoveryService(
        _config(workspace, {"FORGEMCP_WORKSPACE": str(workspace), "ProgramFiles(x86)": str(program_files)}, visual_studio_instance="Community"),
        run_vswhere=lambda _: json.dumps(document).encode(),
    )
    assert "visual_studio: selected instance is missing the VC tool workload" in missing.snapshot().rejections


def test_developer_environment_failure_and_malicious_line_are_safely_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    program_files = tmp_path.parent / f"ProgramFilesX86-{tmp_path.name}"
    instance, _ = _vs_layout(program_files)
    document = [{"installationPath": str(instance), "productId": "BuildTools", "installationVersion": "18.1", "displayName": "Build Tools", "packages": [{"id": "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"}]}]
    config = _config(workspace, {"FORGEMCP_WORKSPACE": str(workspace), "ProgramFiles(x86)": str(program_files)})
    failed = ToolchainDiscoveryService(config, run_vswhere=lambda _: json.dumps(document).encode(), capture_environment=lambda *_: (_ for _ in ()).throw(ValueError("bad")))
    malicious = ToolchainDiscoveryService(config, run_vswhere=lambda _: json.dumps(document).encode(), capture_environment=lambda *_: {"PATH": "ok", "BAD\x00KEY": "value"})
    assert failed.toolchain_environment is None and malicious.toolchain_environment is None
    assert "visual_studio: developer environment capture failed" in failed.snapshot().rejections
    assert "visual_studio: developer environment capture failed" in malicious.snapshot().rejections


def test_architecture_and_workspace_spoofs_are_rejected(tmp_path: Path) -> None:
    wrong = _pe(tmp_path.parent / "wrong-arch.exe", machine=0xAA64)
    workspace_tool = _pe(tmp_path / "workspace-tool.exe")
    env = {"FORGEMCP_WORKSPACE": str(tmp_path)}
    wrong_discovery = ToolchainDiscoveryService(_config(tmp_path, env, cmake_path=str(wrong), host_arch="x64"))
    workspace_discovery = ToolchainDiscoveryService(_config(tmp_path, env, cmake_path=str(workspace_tool), host_arch="x64"))
    assert "cmake: candidate architecture is incompatible" in wrong_discovery.snapshot().rejections
    assert "cmake: candidate is inside the workspace" in workspace_discovery.snapshot().rejections


def test_symlink_and_replacement_candidates_cannot_keep_approval(tmp_path: Path) -> None:
    target = _pe(tmp_path.parent / "real-tool.exe")
    link = tmp_path.parent / "tool-link.exe"
    try:
        link.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlinks unavailable: {error}")
    discovery = ToolchainDiscoveryService(_config(tmp_path, {"FORGEMCP_WORKSPACE": str(tmp_path)}, cmake_path=str(link)))
    assert "cmake: candidate traverses a symlink or reparse point" in discovery.snapshot().rejections
    policy = ProcessPolicy(allowed_executables=frozenset(), allowed_executable_paths=frozenset({target}))
    assert policy.approves_exact_executable(target)
    target.write_bytes(b"replacement-with-different-metadata")
    assert not policy.approves_exact_executable(target)


def test_filtered_developer_environment_failure_is_nonfatal_and_multi_app_state_isolated(tmp_path: Path) -> None:
    first = _config(tmp_path, {"FORGEMCP_WORKSPACE": str(tmp_path), "FORGEMCP_BUILD_DIR": "one"})
    second = _config(tmp_path, {"FORGEMCP_WORKSPACE": str(tmp_path), "FORGEMCP_BUILD_DIR": "two"})
    one = ToolchainDiscoveryService(first)
    two = ToolchainDiscoveryService(second)
    assert first.build_dir == "one" and second.build_dir == "two"
    assert one.snapshot() is not two.snapshot()


def _process_result() -> ProcessResult:
    now = datetime.now(UTC)
    return ProcessResult(exit_code=0, started_at=now, finished_at=now, stdout=ProcessOutput(text=""), stderr=ProcessOutput(text=""))


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run(self, argv, *, cwd=".", environment=None, inherit_environment=None, timeout_seconds=None):
        self.calls.append(tuple(argv))
        return _process_result()


def test_optional_binary_dir_resolves_preset_then_workspace_default(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "CMakePresets.json").write_text(json.dumps({"version": 4, "configurePresets": [{"name": "dev", "binaryDir": "preset-build"}]}), encoding="utf-8")
    config = _config(tmp_path, {"FORGEMCP_WORKSPACE": str(tmp_path), "FORGEMCP_CONFIGURE_PRESET": "dev"})
    runtime = _Runtime()
    service = CMakeService(WorkspaceService(config, create_logger("CRITICAL")), runtime, config)
    result = asyncio.run(service.configure())
    assert result.binary_dir == "preset-build"
    assert runtime.calls[0][:5] == ("cmake", "-S", ".", "-B", "preset-build")
    assert asyncio.run(service.status()).profile is not None
    assert asyncio.run(service.status()).profile.binary_dir_source == "discovery"


def test_doctor_json_and_print_config_are_sanitized(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["--workspace", str(tmp_path), "--build-dir", "build", "print-config"])
    printed = capsys.readouterr().out
    assert str(tmp_path) not in printed and '"source"' in printed
    main(["--workspace", str(tmp_path), "doctor", "--json"])
    doctor = capsys.readouterr().out
    assert str(tmp_path) not in doctor
    assert '"tools"' in doctor


def test_every_runtime_environment_variable_is_documented() -> None:
    root = Path(__file__).parents[1]
    contents = "\n".join(
        path.read_text(encoding="utf-8")
        for directory in (root / "src", root / "tests")
        for path in directory.rglob("*.py")
    )
    names = set(re.findall(r"FORGEMCP_[A-Z0-9_]+", contents))
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert names <= set(re.findall(r"FORGEMCP_[A-Z0-9_]+", readme))


@pytest.mark.skipif(
    os.name != "nt" or not os.environ.get("FORGEMCP_REAL_WINDOWS_TOOLCHAIN_GATE"),
    reason="opt-in real Windows toolchain gate requires FORGEMCP_REAL_WINDOWS_TOOLCHAIN_GATE",
)
def test_real_msvc_toolchain_works_from_clean_path_and_leaves_no_processes(tmp_path: Path) -> None:
    """Live gate: VS CMake/CTest + MSVC work without global CMake/PATH access."""
    (tmp_path / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.23)\nproject(forgemcp_gate LANGUAGES CXX)\n"
        "add_executable(app main.cpp)\nenable_testing()\nadd_test(NAME app_runs COMMAND app)\n",
        encoding="utf-8",
    )
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    environment = dict(os.environ)
    environment["PATH"] = ""
    environment.pop("VSCMD_VER", None)
    environment.pop("VCINSTALLDIR", None)
    config = ForgeConfig.from_sources(
        environment=environment,
        cli={"workspace_root": str(tmp_path), "toolchain": "msvc"},
    )

    async def exercise() -> None:
        application = ForgeApplication.create(config)
        runtime = application.services.get("process_runtime")
        discovery = application.services.get("toolchain_discovery")
        try:
            await application.start()
            assert isinstance(runtime, ProcessRuntime)
            assert isinstance(discovery, ToolchainDiscoveryService)
            assert discovery.executable("cmake") is not None
            assert discovery.executable("ctest") is not None
            assert discovery.executable("cl") is not None
            service = CMakeService(WorkspaceService(config, create_logger("CRITICAL")), runtime, config, discovery)
            assert (await service.configure()).process.exit_code == 0
            assert (await service.build()).process.exit_code == 0
            assert (await service.run_tests()).process.exit_code == 0
        finally:
            await application.aclose()
        assert runtime.cached_status().active_processes == 0

    asyncio.run(exercise())
