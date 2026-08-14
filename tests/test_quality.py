"""Focused fake-tool coverage for the bounded QualityPlugin vertical slice."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.models import ProcessOutput, ProcessResult
from forgemcp.processes import ProcessExecutableError
from forgemcp.processes import ProcessRuntime
from forgemcp.quality import ClangFormatService, ClangTidyService, SanitizerReportParser
from forgemcp.quality.errors import QualityRequestError
from forgemcp.quality.models import SanitizerKind, TidyExecutionState
from forgemcp.workspace import WorkspaceService


def result(*, exit_code: int = 0, stdout: str = "", stderr: str = "", timed_out: bool = False) -> ProcessResult:
    now = datetime.now(UTC)
    return ProcessResult(
        exit_code=None if timed_out else exit_code,
        timed_out=timed_out,
        started_at=now,
        finished_at=now,
        stdout=ProcessOutput(text=stdout),
        stderr=ProcessOutput(text=stderr),
    )


class FakeRuntime:
    def __init__(self, responses: Sequence[ProcessResult | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[bytes | None] = []

    def resolve_executable(self, executable: str) -> str:
        return str(Path("C:/llvm/bin") / (executable + ".exe" if not executable.endswith(".exe") else executable))

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        timeout_seconds: float | None = None,
        input_data: bytes | None = None,
    ) -> ProcessResult:
        self.calls.append(tuple(argv))
        self.inputs.append(input_data)
        response = self.responses.pop(0) if self.responses else ProcessExecutableError("missing")
        if isinstance(response, Exception):
            raise response
        return response


def workspace(root: Path) -> WorkspaceService:
    return WorkspaceService(ForgeConfig(workspace_root=root), create_logger("CRITICAL"))


def replacement_xml(offset: int, length: int, text: str) -> str:
    return f"<replacements xml:space='preserve'><replacement offset='{offset}' length='{length}'>{text}</replacement></replacements>"


FORMAT_HELP = "USAGE: clang-format --output-replacements-xml --assume-filename=<file>\n"
TIDY_HELP = "USAGE: clang-tidy [options] --list-checks --checks=<pattern>\n"


def test_clang_format_check_apply_uses_structured_replacements_and_snapshot_cas(tmp_path: Path):
    source = tmp_path / "main.cpp"
    source.write_bytes(b"int main(){}\n")
    runtime = FakeRuntime([
        result(stdout="clang-format version 18.1.0\n"),
        result(stdout=FORMAT_HELP),
        result(stdout=replacement_xml(10, 0, " ")),
    ])
    service = ClangFormatService(ForgeConfig(workspace_root=tmp_path), workspace(tmp_path), runtime)

    checked = asyncio.run(service.check(["main.cpp"]))

    assert checked.clean is False
    assert checked.files[0].would_change is True
    assert "-i" not in runtime.calls[2]
    assert runtime.calls[2][0] == r"C:\llvm\bin\clang-format.exe"
    assert runtime.calls[2][1:] == ("--output-replacements-xml", "--assume-filename=main.cpp")
    assert runtime.inputs[2] == b"int main(){}\n"
    assert source.read_text(encoding="utf-8") == "int main(){}\n"

    runtime.responses.extend([
        result(stdout="clang-format version 18.1.0\n"),
        result(stdout=FORMAT_HELP),
        result(stdout=replacement_xml(10, 0, " ")),
    ])
    applied = asyncio.run(service.apply([("main.cpp", checked.files[0].snapshot_sha256)]))

    assert applied.applied is True
    assert source.read_text(encoding="utf-8") == "int main() {}\n"


def test_clang_format_apply_is_all_or_nothing_when_one_file_fails(tmp_path: Path):
    first = tmp_path / "one.cpp"
    second = tmp_path / "two.cpp"
    first.write_text("int a(){}\n", encoding="utf-8")
    second.write_text("int b(){}\n", encoding="utf-8")
    service = ClangFormatService(
        ForgeConfig(workspace_root=tmp_path),
        workspace(tmp_path),
        FakeRuntime([
            result(stdout="clang-format version 18.1.0\n"),
            result(stdout=FORMAT_HELP),
            result(stdout=replacement_xml(7, 0, " ")),
            result(exit_code=1),
        ]),
    )
    snapshots = workspace(tmp_path)

    applied = asyncio.run(service.apply([
        ("one.cpp", snapshots.get_snapshot("one.cpp").sha256),
        ("two.cpp", snapshots.get_snapshot("two.cpp").sha256),
    ]))

    assert applied.applied is False
    assert first.read_text(encoding="utf-8") == "int a(){}\n"
    assert second.read_text(encoding="utf-8") == "int b(){}\n"


def test_tidy_normalizes_windows_paths_multiline_notes_and_omits_external(tmp_path: Path):
    source = tmp_path / "main.cpp"
    source.write_text("int main() { return 0; }\n", encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    (build / "compile_commands.json").write_text("[]", encoding="utf-8")
    runtime = FakeRuntime([
        result(stdout="clang-tidy version 18.1.0\n"),
        result(stdout=TIDY_HELP),
        result(stdout=(
            f"{source}:1:5: warning: issue found [modernize-use-nullptr]\n"
            "  more diagnostic context\n"
            "C:\\outside\\lib.cpp:2:1: warning: hidden [x-check]\n"
        )),
    ])
    service = ClangTidyService(ForgeConfig(workspace_root=tmp_path), workspace(tmp_path), runtime)

    parsed = asyncio.run(service.run(paths=["main.cpp"], compile_commands_dir="build"))

    assert parsed.execution_state is TidyExecutionState.COMPLETED
    assert len(parsed.diagnostics) == 1
    assert parsed.diagnostics[0].code == "modernize-use-nullptr"
    assert "more diagnostic context" not in parsed.diagnostics[0].message
    assert parsed.omitted_external_count == 1
    assert runtime.calls[2] == (r"C:\llvm\bin\clang-tidy.exe", "-p=build", r".\main.cpp")


def test_tidy_missing_tool_and_bad_pattern_are_safe(tmp_path: Path):
    service = ClangTidyService(
        ForgeConfig(workspace_root=tmp_path), workspace(tmp_path), FakeRuntime([ProcessExecutableError("missing")])
    )
    assert asyncio.run(service.status()).available is False
    with pytest.raises(QualityRequestError):
        asyncio.run(service.run(paths=["../outside.cpp"], compile_commands_dir="build", checks="--fix"))


def test_sanitizer_parser_handles_partial_multiple_and_workspace_only_frames(tmp_path: Path):
    source = tmp_path / "main.cpp"
    source.write_text("int main() { return 0; }\n", encoding="utf-8")
    parser = SanitizerReportParser(workspace(tmp_path))
    report = (
        "==1==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1234\n"
        f"    #0 0x1234 in main {source}:1:1\n"
        "SUMMARY: AddressSanitizer: heap-buffer-overflow\n"
        "runtime error: signed integer overflow\n"
        "    #0 0x9999 in external C:\\outside\\lib.cpp:2:3\n"
    )

    parsed = parser.parse(report)

    assert [item.kind for item in parsed.findings] == [SanitizerKind.ADDRESS, SanitizerKind.UNDEFINED]
    assert parsed.findings[0].frames[0].location is not None
    assert parsed.findings[1].omitted_external_count == 1
    assert parsed.complete is False
    with pytest.raises(QualityRequestError):
        parser.parse("x" * 65_537)


def _local_llvm_quality_tools() -> tuple[Path, Path] | None:
    """Find a deliberately installed real LLVM formatter/tidy pair for an optional gate."""
    configured_format = os.environ.get("FORGEMCP_CLANG_FORMAT_LIVE_TEST")
    configured_tidy = os.environ.get("FORGEMCP_CLANG_TIDY_LIVE_TEST")
    if configured_format and configured_tidy:
        formatter, tidy = Path(configured_format), Path(configured_tidy)
    else:
        directory = Path(r"C:\Program Files\LLVM\bin")
        formatter, tidy = directory / "clang-format.exe", directory / "clang-tidy.exe"
    return (formatter, tidy) if formatter.is_file() and tidy.is_file() else None


@pytest.mark.skipif(_local_llvm_quality_tools() is None, reason="requires installed clang-format and clang-tidy")
def test_real_llvm_format_apply_and_tidy_compile_commands_gate(tmp_path: Path):
    """Exercise real UTF-8/CRLF/multi-file formatting and trusted-database tidy."""
    tools = _local_llvm_quality_tools()
    assert tools is not None
    formatter, tidy = tools
    config = ForgeConfig(workspace_root=tmp_path, clang_format_path=formatter, clang_tidy_path=tidy)
    service_workspace = WorkspaceService(config, create_logger("CRITICAL"))
    runtime = ProcessRuntime(config, create_logger("CRITICAL"))
    sources = {
        "main.cpp": b"int main(){return 0;}\n",
        "unicode.cpp": "const char* text=\"ž🙂e\u0301\";\n".encode("utf-8"),
        "crlf.cpp": b"int first=0;\r\nint second=1;\r\n",
        "mixed.cpp": b"int first=0;\r\nint second=1;\n",
        "no-final-newline.cpp": b"int value=0;",
        "bom.cpp": b"\xef\xbb\xbfint bom_value=0;\n",
        "empty.cpp": b"",
    }
    for name, data in sources.items():
        (tmp_path / name).write_bytes(data)

    async def exercise() -> None:
        format_service = ClangFormatService(config, service_workspace, runtime)
        checked = await format_service.check(sources)
        assert any(item.would_change for item in checked.files)
        applied = await format_service.apply(
            [(item.path, item.snapshot_sha256) for item in checked.files]
        )
        assert applied.applied is True
        assert (await format_service.check(sources)).clean is True
        assert (tmp_path / "bom.cpp").read_bytes().startswith(b"\xef\xbb\xbf")
        assert b"\r\n" in (tmp_path / "crlf.cpp").read_bytes()
        assert (tmp_path / "empty.cpp").read_bytes() == b""

        build = tmp_path / "build"
        build.mkdir()
        source = tmp_path / "tidy.cpp"
        source.write_bytes(b"int main() { int unused; return 0; }\n")
        clang_driver = tidy.parent / ("clang++.exe" if os.name == "nt" else "clang++")
        if not clang_driver.is_file():
            pytest.skip("real clang-tidy diagnostic gate requires the matching clang++ driver")
        (build / "compile_commands.json").write_text(json.dumps([{
            "directory": str(tmp_path),
            "file": str(source),
            "arguments": [str(clang_driver), "-std=c++17", "-Wall", "-Wextra", "-c", str(source)],
        }]), encoding="utf-8")
        (tmp_path / ".clang-tidy").write_text("Checks: ''\n", encoding="utf-8")
        tidy_service = ClangTidyService(config, service_workspace, runtime)
        status = await tidy_service.status()
        assert status.available is True
        checks = await tidy_service.list_checks("modernize-*")
        assert checks.checks == tuple(sorted(checks.checks))
        run = await tidy_service.run(
            paths=["tidy.cpp"],
            compile_commands_dir="build",
            checks="-*,clang-analyzer-core.*,clang-diagnostic-unused-variable",
            timeout_seconds=30,
        )
        assert run.execution_state is TidyExecutionState.COMPLETED
        assert run.process.exit_code == 0
        assert any(item.code == "clang-diagnostic-unused-variable" for item in run.diagnostics)
        assert run.complete is True

        external_header = tmp_path.parent / f"{tmp_path.name}-external.hpp"
        external_header.write_text("#error external header diagnostic\n", encoding="utf-8")
        external_source = tmp_path / "external.cpp"
        external_source.write_text(
            f'#include "{external_header.name}"\nint main() {{ return 0; }}\n', encoding="utf-8"
        )
        external_build = tmp_path / "external-build"
        external_build.mkdir()
        (external_build / "compile_commands.json").write_text(json.dumps([{
            "directory": str(tmp_path),
            "file": str(external_source),
            "arguments": [
                str(clang_driver),
                f"-I{external_header.parent}",
                "-Wcpp",
                "-c",
                str(external_source),
            ],
        }]), encoding="utf-8")
        try:
            external_run = await tidy_service.run(
                paths=["external.cpp"],
                compile_commands_dir="external-build",
                checks="-*,clang-analyzer-core.*",
                timeout_seconds=30,
            )
            assert external_run.omitted_external_count >= 1
            assert str(external_header) not in str(external_run.model_dump(mode="json"))
        finally:
            external_header.unlink(missing_ok=True)

        timed_out = await tidy_service.run(
            paths=["tidy.cpp"],
            compile_commands_dir="build",
            checks="*",
            timeout_seconds=0.0001,
        )
        assert timed_out.execution_state is TidyExecutionState.TIMED_OUT
        assert runtime._handles == set()
        await runtime.aclose()

    asyncio.run(exercise())
