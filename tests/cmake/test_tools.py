"""Parsing and command construction using deliberately fragmented process output."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forgemcp.toolchain.errors import ToolParserError
from forgemcp.toolchain.tools import cmake, ctest


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Session:
    def __init__(self, text, code=0):
        self.text = text
        self.code = code
        self.closed = False
        self.close_stdin = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def output(self):
        for offset in range(0, len(self.text), 3):
            yield SimpleNamespace(stream="stdout", text=self.text[offset:offset + 3])

    async def wait(self):
        return self.code


@pytest.mark.anyio
async def test_cmake_parses_split_lines_and_preserves_native_arguments(cpp_acceptance_project):
    session = Session(
        'Available configure presets:\r\n  "debug" - Debug\n'
        'Available build presets:\n  "build-debug"\n'
        'Available test presets:\n  "test-debug"'
    )
    launch = AsyncMock(return_value=session)
    methods = cmake.create_spec(
        cpp_acceptance_project / "cmake",
        SimpleNamespace(launch=launch),
        {"MODE": "selected"},
        False,
    ).methods
    presets = await methods["presets"](cpp_acceptance_project)
    assert presets.configure == ["debug"]
    assert presets.build == ["build-debug"]
    assert presets.test == ["test-debug"]
    assert session.closed
    session.text = "-- Configuring done\r\n-- Generating done"
    result = await methods["configure"](cpp_acceptance_project, None, preset="debug")
    assert result.configured and result.generated
    assert launch.call_args.args[1] == ["--preset", "debug"]
    assert launch.call_args.kwargs["env"] == {"MODE": "selected"}
    assert launch.call_args.kwargs["inherit_environment"] is False
    session.text = "[1/2] Building main.cpp\r\n[2/2] Linking app\nFAILED: app"
    session.code = 1
    progress = AsyncMock()
    result = await methods["build"](
        cpp_acceptance_project,
        None,
        preset="build-debug",
        targets=["app"],
        parallel=2,
        on_progress=progress,
    )
    assert (result.completed_steps, result.total_steps, result.return_code) == (2, 2, 1)
    assert "FAILED: app" in result.output_tail
    assert progress.await_args_list[0].args == ("[1/2] Building main.cpp",)
    assert launch.call_args.args[1] == [
        "--build",
        "--preset",
        "build-debug",
        "--target",
        "app",
        "--parallel",
        "2",
    ]


@pytest.mark.anyio
async def test_ctest_report_statuses_and_exact_name_filter(cpp_acceptance_project):
    report = cpp_acceptance_project / "report.xml"
    report.write_text(
        '<testsuite><testcase name="ok" time="0.25"/>'
        '<testcase name="bad"><failure message="assertion"/></testcase>'
        '<testcase name="skip"><skipped/></testcase>'
        '<testcase name="disabled" status="notrun"/></testsuite>'
    )
    launch = AsyncMock(return_value=Session("1/4 Test #1: ok ... Passed\n", 1))
    methods = ctest.create_spec(
        cpp_acceptance_project / "ctest",
        SimpleNamespace(launch=launch),
    ).methods
    result = await methods["test"](
        cpp_acceptance_project,
        None,
        preset="native-test",
        report_path=report,
        names=["case.+(x)"],
    )
    assert [case.status for case in result.tests] == ["passed", "failed", "skipped", "not_run"]
    assert result.tests[0].duration_seconds == 0.25
    assert result.tests[1].message == "assertion"
    arguments = launch.call_args.args[1]
    assert arguments[:2] == ["--preset", "native-test"]
    assert arguments[arguments.index("--tests-regex") + 1] == r"^(case\.\+\(x\))$"
    report.write_text("invalid XML")
    with pytest.raises(ToolParserError):
        ctest.parse_report(report)
