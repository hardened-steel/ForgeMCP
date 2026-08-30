"""Phase D1 kit selection and existing-build-tree safety coverage."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgemcp.cmake import CMakeBuildTreeIncompatibleError, CMakeKitError, CMakePresetKitConflictError, CMakeService
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.models import ProcessOutput, ProcessResult
from forgemcp.toolchain import CMakeKit, CMakeKitList, ToolchainProfile
from forgemcp.workspace import WorkspaceService


def _kit(identifier: str, family: str = "clang") -> CMakeKit:
    return CMakeKit(
        id=identifier,
        display_name=f"{family} x64",
        source="standalone",
        compiler_family=family,
        c_compiler=family,
        cxx_compiler=f"{family}++",
        compiler_version="22.1.0",
        host_arch="x64",
        target_arch="x64",
        environment_profile="none",
        compatible_generators=("Ninja",),
        preferred_generator="Ninja",
        compile_commands="supported",
        debugger_compatibility="compatible",
        readiness="ready",
    )


class _Toolchain:
    def __init__(self, *kits: CMakeKit) -> None:
        self._profiles = {
            item.id: ToolchainProfile(
                item,
                Path(f"C:/safe/{item.c_compiler}.exe"),
                Path(f"C:/safe/{item.cxx_compiler}.exe"),
                None,
            )
            for item in kits
        }

    def kits(self) -> CMakeKitList:
        return CMakeKitList(kits=tuple(self._profiles[key].kit for key in sorted(self._profiles)), discovery_state="cached", complete=True)

    def kit(self, identifier: str) -> CMakeKit | None:
        profile = self._profiles.get(identifier)
        return None if profile is None else profile.kit

    def kit_profile(self, identifier: str) -> ToolchainProfile | None:
        return self._profiles.get(identifier)

    @staticmethod
    def executable(tool: str) -> Path | None:
        return Path("C:/safe/cmake.exe") if tool == "cmake" else None


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def run(self, argv, **_kwargs) -> ProcessResult:
        self.calls.append(tuple(argv))
        now = datetime.now(UTC)
        return ProcessResult(
            exit_code=0, started_at=now, finished_at=now,
            stdout=ProcessOutput(text="C:/host/secret/output\n"),
            stderr=ProcessOutput(text=""),
        )


def _service(root: Path, *kits: CMakeKit) -> tuple[CMakeService, _Runtime]:
    config = ForgeConfig(workspace_root=root)
    runtime = _Runtime()
    return CMakeService(
        WorkspaceService(config, create_logger("CRITICAL")), runtime, config, _Toolchain(*kits)
    ), runtime


def test_selection_is_cas_guarded_and_invalid_selection_does_not_change_state(tmp_path: Path) -> None:
    clang = _kit("kit-clang-000000000001")
    gcc = _kit("kit-gcc-00000000000002", "gcc")
    service, _ = _service(tmp_path, clang, gcc)

    first = asyncio.run(service.select_kit(clang.id, expected_selection_generation=0))
    assert first.selected_kit == clang.id and first.selection_generation == 1
    with pytest.raises(CMakeKitError):
        asyncio.run(service.select_kit("kit-not-present-0000000"))
    assert service._kit_selection().selected_kit == clang.id
    with pytest.raises(Exception) as stale:
        asyncio.run(service.select_kit(gcc.id, expected_selection_generation=0))
    assert getattr(stale.value, "code", None) == "kit_selection_conflict"


def test_invalid_initial_kit_never_falls_back_to_an_automatic_kit(tmp_path: Path) -> None:
    available = _kit("kit-clang-000000000001")
    config = ForgeConfig(workspace_root=tmp_path, cmake_kit="kit-missing-00000000000")
    runtime = _Runtime()
    service = CMakeService(
        WorkspaceService(config, create_logger("CRITICAL")),
        runtime,
        config,
        _Toolchain(available),
    )

    with pytest.raises(CMakeKitError, match="automatic fallback is disabled"):
        asyncio.run(service.configure(binary_dir="build"))
    assert runtime.calls == []


def test_explicit_kit_uses_private_compilers_and_per_kit_directory_without_output_leak(tmp_path: Path) -> None:
    kit = _kit("kit-clang-000000000001")
    service, runtime = _service(tmp_path, kit)

    result = asyncio.run(service.configure(kit=kit.id))

    assert result.binary_dir == f"build/forgemcp/{kit.id}"
    assert result.effective_kit == kit
    assert any(argument.startswith("-DCMAKE_C_COMPILER:FILEPATH=C:") for argument in runtime.calls[0])
    serialized = json.dumps(result.model_dump(mode="json"))
    assert "C:/safe/clang" not in serialized
    assert "C:/host/secret/output" not in serialized


def test_preset_and_explicit_kit_conflict_before_configure(tmp_path: Path) -> None:
    kit = _kit("kit-clang-000000000001")
    service, runtime = _service(tmp_path, kit)
    (tmp_path / "CMakePresets.json").write_text(
        json.dumps({"version": 4, "configurePresets": [{"name": "preset", "binaryDir": "build"}]}),
        encoding="utf-8",
    )

    with pytest.raises(CMakePresetKitConflictError):
        asyncio.run(service.configure(preset="preset", kit=kit.id))
    assert runtime.calls == []


def test_existing_cache_is_read_only_adoptable_or_rejected_by_explicit_kit(tmp_path: Path) -> None:
    kit = _kit("kit-clang-000000000001")
    service, runtime = _service(tmp_path, kit)
    build = tmp_path / "build"
    build.mkdir()
    (build / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={tmp_path}\r\n"
        "CMAKE_GENERATOR:INTERNAL=Ninja\r\n"
        "CMAKE_C_COMPILER:FILEPATH=C:/safe/clang.exe\r\n"
        "CMAKE_CXX_COMPILER:FILEPATH=C:/safe/clang++.exe\r\n",
        encoding="utf-8",
    )

    trees = service.list_build_trees()
    assert trees[0].binary_dir == "build"
    assert trees[0].source_matches_workspace is True
    assert trees[0].compiler_family == "clang"
    assert trees[0].category in {"adoptable", "buildable"}

    msvc = _kit("kit-msvc-00000000000002", "msvc")
    incompatible, _ = _service(tmp_path, msvc)
    with pytest.raises(CMakeBuildTreeIncompatibleError):
        asyncio.run(incompatible.configure(binary_dir="build", kit=msvc.id))
    assert runtime.calls == []


def test_existing_tree_with_same_family_but_different_compiler_is_not_executed(tmp_path: Path) -> None:
    kit = _kit("kit-clang-000000000001")
    service, runtime = _service(tmp_path, kit)
    build = tmp_path / "build"
    build.mkdir()
    (build / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={tmp_path}\r\n"
        "CMAKE_GENERATOR:INTERNAL=Ninja\r\n"
        "CMAKE_C_COMPILER:FILEPATH=C:/other/clang.exe\r\n"
        "CMAKE_CXX_COMPILER:FILEPATH=C:/other/clang++.exe\r\n",
        encoding="utf-8",
    )

    with pytest.raises(CMakeBuildTreeIncompatibleError):
        asyncio.run(service.build(binary_dir="build"))
    assert runtime.calls == []


def test_msvc_and_linker_diagnostics_are_path_safe_and_structured(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, _kit("kit-clang-000000000001"))
    source = tmp_path / "main.cpp"
    source.write_text("int main() {}\n", encoding="utf-8")
    now = datetime.now(UTC)
    result = ProcessResult(
        exit_code=1, started_at=now, finished_at=now,
        stdout=ProcessOutput(text=f"{source}(1,5): error C2143: syntax error\nLINK : fatal error LNK1104: cannot open C:/secret/lib.lib\n"),
        stderr=ProcessOutput(text=""),
    )

    diagnostics, omitted, invalid, complete = service._safe_diagnostics(result, category="build")

    assert [item.category for item in diagnostics] == ["compiler", "linker"]
    assert diagnostics[0].file == "main.cpp" and diagnostics[0].line == 0 and diagnostics[0].column == 4
    assert diagnostics[1].code == "LNK1104" and "C:/secret" not in diagnostics[1].message
    assert (omitted, invalid, complete) == (0, 0, True)


def test_lld_link_failure_is_classified_without_disclosing_its_absolute_path(tmp_path: Path) -> None:
    service, _ = _service(tmp_path, _kit("kit-clang-000000000001"))
    now = datetime.now(UTC)
    result = ProcessResult(
        exit_code=1,
        started_at=now,
        finished_at=now,
        stdout=ProcessOutput(text=""),
        stderr=ProcessOutput(
            text="lld-link: error: undefined symbol: C:/host/canary/intentionally_undefined_symbol\n"
        ),
    )

    diagnostics, omitted, invalid, complete = service._safe_diagnostics(result, category="build")

    assert len(diagnostics) == 1 and diagnostics[0].category == "linker"
    assert "C:/host/canary" not in diagnostics[0].message
    assert service._build_outcome(result, diagnostics) == "linker_failure"
    assert (omitted, invalid, complete) == (0, 0, True)
