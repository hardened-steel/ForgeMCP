"""Adversarial security and integration regression for Quality Phase 1."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forgemcp.core.application import ForgeApplication
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.models import ProcessOutput, ProcessResult
from forgemcp.processes import ProcessExecutableError, ProcessRuntime
from forgemcp.plugins import PluginManager
from forgemcp.quality.clang_format import (
    MAX_FORMAT_XML_CHARACTERS,
    ClangFormatService,
    _Replacement,
    _byte_positions,
)
from forgemcp.quality.clang_tidy import ClangTidyService
from forgemcp.quality.errors import QualityRequestError
from forgemcp.quality.models import SanitizerKind, TidyExecutionState
from forgemcp.quality.sanitizer import (
    MAX_SANITIZER_FRAMES,
    MAX_SANITIZER_INPUT_CHARACTERS,
    SanitizerReportParser,
)
from forgemcp.workspace import SymlinkWorkspacePathError, WorkspaceService


FORMAT_HELP = "USAGE: clang-format --output-replacements-xml --assume-filename=<file>\n"
TIDY_HELP = "USAGE: clang-tidy [options] --list-checks --checks=<pattern>\n"


def process_result(
    *,
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
) -> ProcessResult:
    now = datetime.now(UTC)
    return ProcessResult(
        exit_code=None if timed_out else exit_code,
        timed_out=timed_out,
        started_at=now,
        finished_at=now,
        stdout=ProcessOutput(text=stdout, truncated=stdout_truncated),
        stderr=ProcessOutput(text=stderr, truncated=stderr_truncated),
    )


def service_workspace(root: Path) -> WorkspaceService:
    return WorkspaceService(ForgeConfig(workspace_root=root), create_logger("CRITICAL"))


def replacement_xml(*replacements: tuple[int, int, str], incomplete: str = "false") -> str:
    nodes = "".join(
        f"<replacement offset='{offset}' length='{length}'>{text}</replacement>"
        for offset, length, text in replacements
    )
    return (
        "<replacements xml:space='preserve' "
        f"incomplete_format='{incomplete}'>{nodes}</replacements>"
    )


class RoutingRuntime:
    """Deterministic fake that exposes exact argv/input without raw-output logging."""

    def __init__(
        self,
        formatter: Callable[[bytes], ProcessResult] | None = None,
        tidy_result: ProcessResult | None = None,
    ) -> None:
        self.formatter = formatter
        self.tidy_result = tidy_result
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[bytes | None] = []

    def resolve_executable(self, executable: str) -> str:
        name = Path(executable).name
        if name in {"clang-format", "clang-tidy"}:
            name += ".exe"
        return str(Path("C:/trusted/llvm") / name)

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
        await asyncio.sleep(0)
        if "--version" in argv:
            banner = "clang-format version 22.1.8\n" if "format" in Path(argv[0]).name else "LLVM version 22.1.8\n"
            return process_result(stdout=banner)
        if "--help" in argv:
            return process_result(stdout=FORMAT_HELP if "format" in Path(argv[0]).name else TIDY_HELP)
        if "--output-replacements-xml" in argv:
            assert self.formatter is not None
            return self.formatter(input_data or b"")
        assert self.tidy_result is not None
        return self.tidy_result


def test_quality_path_discovery_skips_current_workspace_and_uses_exact_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace_root = tmp_path / "workspace"
    trusted = tmp_path / "trusted"
    workspace_root.mkdir()
    trusted.mkdir()
    executable_name = "clang-format.exe" if os.name == "nt" else "clang-format"
    workspace_spoof = workspace_root / executable_name
    trusted_tool = trusted / executable_name
    shutil.copy2(sys.executable, workspace_spoof)
    shutil.copy2(sys.executable, trusted_tool)
    workspace_spoof.chmod(0o755)
    trusted_tool.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((".", str(workspace_root), str(trusted))),
    )

    runtime = ProcessRuntime(ForgeConfig(workspace_root=workspace_root), create_logger("CRITICAL"))

    assert Path(runtime.resolve_executable("clang-format")) == trusted_tool.resolve()
    assert runtime.policy.approves_exact_executable(trusted_tool)
    assert not runtime.policy.approves_exact_executable(workspace_spoof)

    trusted_tool.write_bytes(b"replaced after approval")
    with pytest.raises(ProcessExecutableError):
        runtime.resolve_executable("clang-format")
    asyncio.run(runtime.aclose())


def test_quality_path_discovery_rejects_symlink_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from forgemcp.processes.runtime import _discover_quality_on_path

    workspace_root = tmp_path / "workspace"
    candidates = tmp_path / "candidates"
    workspace_root.mkdir()
    candidates.mkdir()
    name = "clang-format.exe" if os.name == "nt" else "clang-format"
    link = candidates / name
    try:
        link.symlink_to(Path(sys.executable).resolve())
    except OSError:
        pytest.skip("the host does not permit executable symlink creation")

    assert _discover_quality_on_path("clang-format", str(candidates), workspace_root) is None


@pytest.mark.parametrize(
    "banner,help_text",
    [
        ("clang-format version " + "x" * 257 + "\n", FORMAT_HELP),
        ("clang-format version 22.1.8\n", "USAGE: unrelated-tool\n"),
        ("not a version\n", FORMAT_HELP),
    ],
)
def test_formatter_qualification_rejects_malformed_oversized_or_wrong_tool(
    tmp_path: Path, banner: str, help_text: str
):
    class ProbeRuntime(RoutingRuntime):
        async def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
            if "--version" in argv:
                return process_result(stdout=banner)
            if "--help" in argv:
                return process_result(stdout=help_text)
            raise ProcessExecutableError("missing")

    status = asyncio.run(
        ClangFormatService(
            ForgeConfig(workspace_root=tmp_path), service_workspace(tmp_path), ProbeRuntime()
        ).status()
    )
    assert status.available is False
    assert status.executable is None


def test_tidy_qualification_rejects_a_different_llvm_tool_under_the_right_name(tmp_path: Path):
    class WrongToolRuntime(RoutingRuntime):
        async def run(self, argv: Sequence[str], **kwargs: object) -> ProcessResult:
            if "--version" in argv:
                return process_result(stdout="LLVM version 22.1.8\n")
            if "--help" in argv:
                return process_result(stdout=FORMAT_HELP)
            raise ProcessExecutableError("missing")

    status = asyncio.run(
        ClangTidyService(
            ForgeConfig(workspace_root=tmp_path), service_workspace(tmp_path), WrongToolRuntime()
        ).status()
    )
    assert status.available is False


def test_quality_status_never_discloses_resolved_executable_paths(tmp_path: Path):
    runtime = RoutingRuntime()
    workspace = service_workspace(tmp_path)

    async def exercise() -> None:
        formatter = await ClangFormatService(
            ForgeConfig(workspace_root=tmp_path), workspace, runtime
        ).status()
        tidy = await ClangTidyService(
            ForgeConfig(workspace_root=tmp_path), workspace, runtime
        ).status()
        assert formatter.executable == "clang-format"
        assert tidy.executable == "clang-tidy"
        serialized = formatter.model_dump_json() + tidy.model_dump_json()
        assert "C:/trusted" not in serialized and "C:\\\\trusted" not in serialized


@pytest.mark.parametrize(
    "xml",
    [
        "<replacements>",
        "<!DOCTYPE replacements [<!ENTITY x 'expanded'>]><replacements><replacement offset='0' length='0'>&x;</replacement></replacements>",
        "<!DOCTYPE replacements SYSTEM 'file:///private/secret'><replacements/>",
        "<wrong/>",
        "<replacements incomplete_format='true'/>",
        "<replacements extra='x'/>",
        "<replacements>unexpected</replacements>",
        "<replacements><unexpected/></replacements>",
        "<replacements><replacement length='0'/></replacements>",
        "<replacements><replacement offset='0'/></replacements>",
        "<replacements><replacement offset='-1' length='0'/></replacements>",
        "<replacements><replacement offset='0' length='-1'/></replacements>",
        "<replacements><replacement offset='0' length='0' extra='x'/></replacements>",
        "<replacements><replacement offset='0' length='0'><child/></replacement></replacements>",
        "<replacements><replacement offset='1' length='3'/></replacements>",
        "<replacements><replacement offset='0' length='2'/><replacement offset='1' length='0'/></replacements>",
        "<replacements><replacement offset='1' length='0'>a</replacement><replacement offset='1' length='0'>b</replacement></replacements>",
        "<replacements><replacement offset='999999999999999999999' length='0'/></replacements>",
    ],
)
def test_formatter_xml_parser_rejects_untrusted_grammar_and_ranges(xml: str):
    with pytest.raises((ValueError, ET.ParseError)) as captured:
        ClangFormatService._parse_replacements(xml, source_size=3)
    assert not isinstance(captured.value, KeyError)


def test_formatter_xml_parser_supports_escaping_empty_delete_and_eof_insert():
    parsed = ClangFormatService._parse_replacements(
        replacement_xml((0, 0, "&lt;&amp;&gt;"), (3, 0, "")), source_size=3
    )
    assert parsed == (_Replacement(0, 0, "<&>"), _Replacement(3, 0, ""))
    deleted = ClangFormatService._parse_replacements(
        replacement_xml((0, 3, "")), source_size=3
    )
    assert ClangFormatService._apply_replacements("abc", deleted) == ""


def test_formatter_xml_parser_has_an_independent_size_bound():
    with pytest.raises(ValueError, match="limit"):
        ClangFormatService._parse_replacements(
            " " * (MAX_FORMAT_XML_CHARACTERS + 1), source_size=0
        )


def test_formatter_byte_offsets_cover_unicode_non_bmp_combining_bom_and_newlines():
    source = "\ufeffž🙂e\u0301\r\nA\nlast"
    positions = _byte_positions(source)
    encoded = source.encode("utf-8")
    emoji_start = encoded.index("🙂".encode("utf-8"))
    combining_start = encoded.index("\u0301".encode("utf-8"))
    eof = len(encoded)

    assert positions[emoji_start].column == 2
    assert positions[emoji_start + 4].column == 3
    assert positions[combining_start].column == 4
    assert positions[combining_start + 2].column == 5
    assert positions[eof].line == 2
    assert positions[eof].column == 4
    with pytest.raises(KeyError):
        _ = positions[emoji_start + 1]


def test_formatter_workspace_edits_match_byte_application_for_multiple_unicode_replacements(
    tmp_path: Path,
):
    source = "ž🙂x\r\ny\n"
    target = tmp_path / "unicode.cpp"
    target.write_bytes(source.encode("utf-8"))
    data = source.encode("utf-8")
    x_offset = data.index(b"x")
    y_offset = data.index(b"y")
    replacements = (
        _Replacement(x_offset, 1, "X"),
        _Replacement(y_offset, 1, "Y"),
        _Replacement(len(data), 0, "// eof"),
    )
    workspace = service_workspace(tmp_path)
    expected_text = ClangFormatService._apply_replacements(source, replacements)

    result = workspace.apply_text_edits(
        {"unicode.cpp": ClangFormatService._workspace_edits(source, replacements)},
        {"unicode.cpp": workspace.get_snapshot("unicode.cpp")},
    )

    assert result.applied is True
    assert target.read_bytes() == expected_text.encode("utf-8")


def test_formatter_empty_file_is_a_clean_noop(tmp_path: Path):
    (tmp_path / "empty.cpp").write_bytes(b"")
    runtime = RoutingRuntime(formatter=lambda data: process_result(stdout=""))
    checked = asyncio.run(
        ClangFormatService(
            ForgeConfig(workspace_root=tmp_path), service_workspace(tmp_path), runtime
        ).check(["empty.cpp"])
    )
    assert checked.clean is True
    assert checked.files[0].would_change is False
    assert runtime.inputs[-1] == b""


def test_formatter_rejects_duplicate_case_paths_on_windows(tmp_path: Path):
    if os.name != "nt":
        pytest.skip("Windows path identities are case-insensitive")
    service = ClangFormatService(
        ForgeConfig(workspace_root=tmp_path), service_workspace(tmp_path), RoutingRuntime()
    )
    with pytest.raises(QualityRequestError, match="more than once"):
        asyncio.run(service.check(["Main.cpp", "main.cpp"]))


def test_formatter_rejects_lexically_duplicate_paths(tmp_path: Path):
    service = ClangFormatService(
        ForgeConfig(workspace_root=tmp_path), service_workspace(tmp_path), RoutingRuntime()
    )
    with pytest.raises(QualityRequestError, match="more than once"):
        asyncio.run(service.check(["main.cpp", f".{os.sep}main.cpp"]))


def test_formatter_option_like_path_is_only_an_assume_filename_value(tmp_path: Path):
    path = "--style=file.cpp"
    (tmp_path / path).write_bytes(b"int value;\n")
    runtime = RoutingRuntime(
        formatter=lambda data: process_result(stdout=replacement_xml())
    )
    checked = asyncio.run(
        ClangFormatService(
            ForgeConfig(workspace_root=tmp_path), service_workspace(tmp_path), runtime
        ).check([path])
    )
    assert checked.clean is True
    argv = runtime.calls[-1]
    assert path not in argv
    assert f"--assume-filename={path}" in argv
    assert not any(value in {"-i", "--style", "--config"} for value in argv)


def test_formatter_uses_snapshot_stdin_and_conflicts_if_disk_changes_during_format(
    tmp_path: Path,
):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"int main(){}\n")

    def format_and_race(data: bytes) -> ProcessResult:
        assert data == b"int main(){}\n"
        target.write_bytes(b"externally changed\n")
        return process_result(stdout=replacement_xml((10, 0, " ")))

    runtime = RoutingRuntime(formatter=format_and_race)
    workspace = service_workspace(tmp_path)
    expected = workspace.get_snapshot("main.cpp").sha256
    applied = asyncio.run(
        ClangFormatService(ForgeConfig(workspace_root=tmp_path), workspace, runtime).apply(
            [("main.cpp", expected)]
        )
    )
    assert applied.applied is False
    assert applied.conflict is True
    assert target.read_bytes() == b"externally changed\n"


def test_formatter_noop_file_still_participates_in_final_snapshot_validation(tmp_path: Path):
    target = tmp_path / "clean.cpp"
    target.write_bytes(b"int value;\n")
    workspace = service_workspace(tmp_path)
    expected = workspace.get_snapshot("clean.cpp").sha256

    def race_noop(data: bytes) -> ProcessResult:
        target.write_bytes(b"int external;\n")
        return process_result(stdout=replacement_xml())

    result = asyncio.run(
        ClangFormatService(
            ForgeConfig(workspace_root=tmp_path),
            workspace,
            RoutingRuntime(formatter=race_noop),
        ).apply([("clean.cpp", expected)])
    )
    assert result.applied is False
    assert result.conflict is True
    assert target.read_bytes() == b"int external;\n"


def test_two_concurrent_formatter_applies_of_one_snapshot_have_one_winner(tmp_path: Path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"int main(){}\n")
    runtime = RoutingRuntime(
        formatter=lambda data: process_result(stdout=replacement_xml((10, 0, " ")))
    )
    workspace = service_workspace(tmp_path)
    service = ClangFormatService(ForgeConfig(workspace_root=tmp_path), workspace, runtime)
    expected = workspace.get_snapshot("main.cpp").sha256

    async def exercise():
        return await asyncio.gather(
            service.apply([("main.cpp", expected)]),
            service.apply([("main.cpp", expected)]),
        )

    results = asyncio.run(exercise())
    assert sorted((item.applied, item.conflict) for item in results) == [(False, True), (True, False)]
    assert target.read_bytes() == b"int main() {}\n"


def test_formatter_check_can_overlap_apply_without_writing_from_check(tmp_path: Path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"int main(){}\n")
    runtime = RoutingRuntime(
        formatter=lambda data: process_result(
            stdout=replacement_xml((10, 0, " ")) if b"main(){}" in data else replacement_xml()
        )
    )
    workspace = service_workspace(tmp_path)
    service = ClangFormatService(ForgeConfig(workspace_root=tmp_path), workspace, runtime)
    expected = workspace.get_snapshot("main.cpp").sha256

    async def exercise():
        return await asyncio.gather(
            service.check(["main.cpp"]), service.apply([("main.cpp", expected)])
        )

    checked, applied = asyncio.run(exercise())
    assert checked.files[0].error is None
    assert applied.applied is True
    assert target.read_bytes() == b"int main() {}\n"


def test_cancelling_formatter_before_process_completion_never_commits(tmp_path: Path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"int main(){}\n")
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingRuntime(RoutingRuntime):
        async def run(
            self,
            argv: Sequence[str],
            *,
            cwd: str = ".",
            timeout_seconds: float | None = None,
            input_data: bytes | None = None,
        ) -> ProcessResult:
            if "--version" in argv:
                return process_result(stdout="clang-format version 22.1.8\n")
            if "--help" in argv:
                return process_result(stdout=FORMAT_HELP)
            entered.set()
            await release.wait()
            return process_result(stdout=replacement_xml((10, 0, " ")))

    workspace = service_workspace(tmp_path)
    service = ClangFormatService(
        ForgeConfig(workspace_root=tmp_path), workspace, BlockingRuntime()
    )
    expected = workspace.get_snapshot("main.cpp").sha256

    async def exercise() -> None:
        operation = asyncio.create_task(service.apply([("main.cpp", expected)]))
        await entered.wait()
        operation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await operation

    asyncio.run(exercise())
    assert target.read_bytes() == b"int main(){}\n"


def test_formatter_timeout_and_truncated_output_never_write(tmp_path: Path):
    target = tmp_path / "main.cpp"
    target.write_bytes(b"int main(){}\n")
    expected = service_workspace(tmp_path).get_snapshot("main.cpp").sha256
    for formatted_result in (
        process_result(timed_out=True),
        process_result(stdout="<replacements/>", stdout_truncated=True),
        process_result(stdout="<replacements/>", stderr_truncated=True),
    ):
        service = ClangFormatService(
            ForgeConfig(workspace_root=tmp_path),
            service_workspace(tmp_path),
            RoutingRuntime(formatter=lambda data, value=formatted_result: value),
        )
        result = asyncio.run(service.apply([("main.cpp", expected)]))
        assert result.applied is False
        assert target.read_bytes() == b"int main(){}\n"


@pytest.mark.parametrize(
    "checks",
    [
        "--fix",
        "--fix-errors",
        "--load=plugin",
        "--extra-arg=-Xclang",
        "--extra-arg-before=-include",
        "--config={}",
        "--config-file=x",
        "--header-filter=.*",
        "@response",
        "check -- -Xclang",
        "x\x00y",
        "x" * 1025,
    ],
)
def test_tidy_checks_cannot_inject_any_argument_surface(checks: str):
    with pytest.raises(QualityRequestError):
        ClangTidyService._validate_checks(checks)


def test_tidy_argv_uses_canonical_tool_fixed_options_and_safe_source_prefixes(tmp_path: Path):
    for name in ("-option.cpp", "@response.cpp"):
        (tmp_path / name).write_bytes(b"int value;\n")
    build = tmp_path / "build"
    build.mkdir()
    (build / "compile_commands.json").write_text("[]", encoding="utf-8")
    runtime = RoutingRuntime(tidy_result=process_result())

    result = asyncio.run(
        ClangTidyService(
            ForgeConfig(workspace_root=tmp_path), service_workspace(tmp_path), runtime
        ).run(
            paths=["-option.cpp", "@response.cpp"],
            compile_commands_dir="build",
            checks="modernize-*,-modernize-use-trailing-return-type",
        )
    )

    assert result.execution_state is TidyExecutionState.COMPLETED
    argv = runtime.calls[-1]
    assert argv == (
        str(Path(r"C:\trusted\llvm\clang-tidy.exe")),
        "-p=build",
        "--checks=modernize-*,-modernize-use-trailing-return-type",
        f".{os.sep}-option.cpp",
        f".{os.sep}@response.cpp",
    )
    assert "--" not in argv
    assert not any(
        value.startswith(
            (
                "--fix",
                "--load",
                "--extra-arg",
                "--config",
                "--header-filter",
            )
        )
        for value in argv
    )


def test_tidy_rejects_duplicate_case_paths_on_windows(tmp_path: Path):
    if os.name != "nt":
        pytest.skip("Windows path identities are case-insensitive")
    with pytest.raises(QualityRequestError, match="more than once"):
        ClangTidyService._validate_paths(["Main.cpp", "main.cpp"])


def test_tidy_requires_regular_non_symlink_compile_commands(tmp_path: Path):
    source = tmp_path / "main.cpp"
    source.write_bytes(b"int main() {}\n")
    build = tmp_path / "build"
    build.mkdir()
    external = tmp_path / "external.json"
    external.write_text("[]", encoding="utf-8")
    try:
        (build / "compile_commands.json").symlink_to(external)
    except OSError:
        pytest.skip("the host does not permit symlink creation")
    service = ClangTidyService(
        ForgeConfig(workspace_root=tmp_path), service_workspace(tmp_path), RoutingRuntime()
    )
    with pytest.raises(SymlinkWorkspacePathError):
        asyncio.run(service.run(paths=["main.cpp"], compile_commands_dir="build"))


def test_tidy_parser_handles_windows_relative_unicode_severities_and_source_lines(tmp_path: Path):
    directory = tmp_path / "dir with space (generated)"
    directory.mkdir()
    source = directory / "colon name.cpp"
    source.write_bytes("ž🙂target\nsecond\n".encode("utf-8"))
    workspace = service_workspace(tmp_path)
    service = ClangTidyService(ForgeConfig(workspace_root=tmp_path), workspace, RoutingRuntime())
    target_byte_column = len("ž🙂".encode("utf-8")) + 1
    relative = source.relative_to(tmp_path).as_posix()
    text = (
        f"\x1b[31m{source}:1:{target_byte_column}: warning: Unicode žinutė [modernize-test]\x1b[0m\n"
        "  continued diagnostic context\n"
        "  ž🙂target\n"
        "  ^~~~~\n"
        f"{relative}:2:1: note: note text\n"
        f"{relative}:2:1: remark: remark text\n"
        f"{relative}:2:1: error: error text\n"
        f"{relative}:2:1: fatal error: fatal text\n"
        f"{relative}:2:1: warning: no check name\n"
        f"{relative}:2:1: warning: duplicate [duplicate-check]\n"
        f"{relative}:2:1: warning: duplicate [duplicate-check]\n"
        f"{relative}:2:1: warning: could not open 'C:\\private folder\\secret.hpp' [path-check]\n"
        "C:\\external folder\\secret.cpp:1:1: warning: hidden [external-check]\n"
        "\\\\server\\share\\secret.cpp:1:1: error: hidden UNC\n"
        f"{relative}:0:1: warning: invalid zero line\n"
        f"{relative}:-1:1: warning: invalid negative line\n"
        f"{relative}:999999999999999999999:1: warning: invalid huge line\n"
        f"{relative}(2,1) : warning: unsupported MSVC format\n"
        f"{relative}:2: warning: missing column\n"
    )

    diagnostics, external, invalid, truncated = service._parse_diagnostics(text)

    assert len(diagnostics) == 9
    assert diagnostics[0].location.range.start.column == 2
    assert diagnostics[0].message == "Unicode žinutė"
    assert "target" not in diagnostics[0].message
    assert [item.severity.value for item in diagnostics[1:5]] == [
        "information",
        "information",
        "error",
        "error",
    ]
    assert diagnostics[5].code is None
    assert diagnostics[-2] == diagnostics[-3]
    assert diagnostics[-1].message == "could not open '<external-path>'"
    assert "secret.hpp" not in diagnostics[-1].message
    assert service._parse_diagnostics(
        f"{relative}:2:1: warning: division / by zero [operator-check]\n"
    )[0][0].message == "division / by zero"
    assert external == 2
    assert invalid == 5
    assert truncated is False


def test_tidy_parser_rejects_byte_column_inside_multibyte_character(tmp_path: Path):
    source = tmp_path / "unicode.cpp"
    source.write_bytes("žx\n".encode("utf-8"))
    service = ClangTidyService(
        ForgeConfig(workspace_root=tmp_path), service_workspace(tmp_path), RoutingRuntime()
    )
    diagnostics, external, invalid, truncated = service._parse_diagnostics(
        f"{source}:1:2: warning: split character [unicode-check]\n"
    )
    assert diagnostics == ()
    assert external == 0
    assert invalid == 1
    assert truncated is False


@pytest.mark.parametrize("exit_code,state", [(0, TidyExecutionState.COMPLETED), (1, TidyExecutionState.TOOL_FAILURE)])
def test_tidy_run_distinguishes_findings_failure_streams_and_truncation(
    tmp_path: Path, exit_code: int, state: TidyExecutionState
):
    source = tmp_path / "main.cpp"
    source.write_bytes(b"int main() {}\n")
    build = tmp_path / "build"
    build.mkdir()
    (build / "compile_commands.json").write_text("[]", encoding="utf-8")
    runtime = RoutingRuntime(
        tidy_result=process_result(
            exit_code=exit_code,
            stdout=f"{source}:1:1: warning: stdout finding [stdout-check]\n",
            stderr=f"{source}:1:1: error: stderr finding [stderr-check]\n",
            stderr_truncated=True,
        )
    )
    result = asyncio.run(
        ClangTidyService(
            ForgeConfig(workspace_root=tmp_path), service_workspace(tmp_path), runtime
        ).run(paths=["main.cpp"], compile_commands_dir="build")
    )
    assert [item.code for item in result.diagnostics] == ["stdout-check", "stderr-check"]
    assert result.execution_state is state
    assert result.truncated is True
    assert result.complete is False
    serialized = result.model_dump(mode="json")
    assert not any(key in serialized for key in ("stdout", "stderr", "compile_command", "environment"))


def test_tidy_timeout_is_not_reported_as_success(tmp_path: Path):
    (tmp_path / "main.cpp").write_bytes(b"int main() {}\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "compile_commands.json").write_text("[]", encoding="utf-8")
    runtime = RoutingRuntime(tidy_result=process_result(timed_out=True))
    result = asyncio.run(
        ClangTidyService(
            ForgeConfig(workspace_root=tmp_path), service_workspace(tmp_path), runtime
        ).run(paths=["main.cpp"], compile_commands_dir="build")
    )
    assert result.execution_state is TidyExecutionState.TIMED_OUT
    assert result.process.timed_out is True


def test_sanitizer_parser_bounds_frames_hides_external_paths_and_does_not_echo_secrets(tmp_path: Path):
    directory = tmp_path / "source with spaces (x)"
    directory.mkdir()
    source = directory / "main.cpp"
    source.write_bytes("ž🙂line\n".encode("utf-8"))
    parser = SanitizerReportParser(service_workspace(tmp_path))
    column = len("ž🙂".encode("utf-8")) + 1
    report = (
        "\x1b[31m==1==ERROR: AddressSanitizer: heap-use-after-free token=TOP_SECRET on address 0x1234\x1b[0m\n"
        f"    #0 0x1234 in main {source}:1:{column}\n"
        "    #1 0x2345 module.dll!fünc+0x20\n"
        "    #2 0x3456\n"
        "    #3 0x4567 in C:\\private\\secret.cpp\n"
        "    #4 0x5678 in hidden /private/secret.cpp:2:3\n"
        "    #5 0x6789 in hidden relative-secret.cpp:2:3\n"
        "SUMMARY: AddressSanitizer: heap-use-after-free TOP_SECRET\n"
    )

    parsed = parser.parse(report)

    finding = parsed.findings[0]
    assert finding.kind is SanitizerKind.ADDRESS
    assert finding.category == "heap-use-after-free"
    assert finding.summary == "AddressSanitizer reported heap-use-after-free."
    assert finding.frames[0].location is not None
    assert finding.frames[0].location.range.start.column == 2
    assert finding.frames[1].function == "module.dll!fünc+0x20"
    assert finding.frames[2].function is None
    assert finding.omitted_external_count == 3
    payload = str(parsed.model_dump(mode="json"))
    assert "TOP_SECRET" not in payload
    assert "private" not in payload


def test_sanitizer_parser_marks_partial_malformed_and_frame_caps(tmp_path: Path):
    parser = SanitizerReportParser(service_workspace(tmp_path))
    frames = "".join(f"    #{index} 0x{index + 1:x}\n" for index in range(MAX_SANITIZER_FRAMES + 2))
    parsed = parser.parse(
        "ERROR: AddressSanitizer: stack-buffer-overflow\n" + frames + "SUMMARY: done\n"
    )
    assert len(parsed.findings[0].frames) == MAX_SANITIZER_FRAMES
    assert parsed.findings[0].truncated is True
    assert parsed.findings[0].complete is False
    assert parsed.truncated is True
    assert parsed.complete is False


def test_sanitizer_unknown_and_ubsan_fallbacks_never_copy_raw_input(tmp_path: Path):
    parser = SanitizerReportParser(service_workspace(tmp_path))
    unknown = parser.parse("unrecognized TOP_SECRET C:\\private\\secret.cpp")
    assert unknown.findings[0].summary == "Unrecognized sanitizer report format."
    assert "TOP_SECRET" not in str(unknown.model_dump(mode="json"))

    ubsan = parser.parse("runtime error: signed integer overflow TOP_SECRET=123\n")
    assert ubsan.findings[0].category == "signed-integer-overflow"
    assert ubsan.findings[0].summary == (
        "UndefinedBehaviorSanitizer reported signed-integer-overflow."
    )
    assert "TOP_SECRET" not in str(ubsan.model_dump(mode="json"))


def test_sanitizer_input_and_symbol_strings_are_bounded(tmp_path: Path):
    parser = SanitizerReportParser(service_workspace(tmp_path))
    exact = parser.parse("x" * MAX_SANITIZER_INPUT_CHARACTERS)
    assert exact.findings[0].kind is SanitizerKind.UNKNOWN
    with pytest.raises(QualityRequestError):
        parser.parse("x" * (MAX_SANITIZER_INPUT_CHARACTERS + 1))

    long_symbol = "f" * 2048
    parsed = parser.parse(
        "ERROR: AddressSanitizer: deadly-signal\n"
        f"    #0 0x1 {long_symbol}\n"
        "SUMMARY: done\n"
    )
    assert len(parsed.findings[0].frames[0].function or "") == 1024
    assert parsed.findings[0].truncated is True


def test_quality_plugin_and_runtime_state_do_not_cross_application_instances(tmp_path: Path):
    async def exercise() -> None:
        first = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        second = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        first_plugins = first.services.get("plugins")
        second_plugins = second.services.get("plugins")
        assert isinstance(first_plugins, PluginManager)
        assert isinstance(second_plugins, PluginManager)
        assert first_plugins is not second_plugins
        assert first.services.get("process_runtime") is not second.services.get("process_runtime")

        await first.start()
        assert any(status.plugin_id == "quality" and status.state.value == "running" for status in first_plugins.statuses())
        await first.aclose()
        assert any(status.plugin_id == "quality" and status.state.value == "stopped" for status in first_plugins.statuses())
        assert all(not item.name.startswith(("quality__", "clang_format__", "clang_tidy__", "sanitizer__")) for item in first_plugins.tools.contributions())

        await second.start()
        assert any(item.name == "quality__status" for item in second_plugins.tools.contributions())
        await second.aclose()

    asyncio.run(exercise())
