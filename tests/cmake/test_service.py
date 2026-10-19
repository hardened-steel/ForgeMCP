"""Operator choices and CMake orchestration, without host tool discovery."""
import asyncio
from collections.abc import AsyncGenerator
import json
import os
from pathlib import Path
import shutil
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import Client

from forgemcp.cmake.build_tree import execution_path
from forgemcp.cmake.errors import CMakeError
from forgemcp.cmake.profiles import ProfileDefinition, parse_profiles
from forgemcp.process.models import ProcessOutput
from forgemcp.toolchain.errors import ToolCommandError
from forgemcp.toolchain.tools import cmake, ctest
from forgemcp.workspace.path import WorkspacePath

from tests.cmake.conftest import server, write_generation


@pytest.fixture
def anyio_backend():
    """Run async tests on the asyncio backend used by the process and LSP services."""
    return "asyncio"


@pytest.fixture
def test_program(request):
    """Copy a real Python executable into the isolated project as a deterministic test program."""
    scenario = request.getfixturevalue("setup")
    folder = scenario.workspace.root / "Test tools & spaces"
    folder.mkdir()
    executable = folder / ("python.exe" if os.name == "nt" else "python")
    shutil.copy2(sys.executable, executable)
    configuration = Path(sys.executable).parent.parent / "pyvenv.cfg"
    if configuration.is_file():
        shutil.copy2(configuration, folder / "pyvenv.cfg")
    return WorkspacePath("project/" + executable.relative_to(scenario.workspace.root).as_posix())


@pytest.mark.anyio
async def test_direct_run_metadata(request):
    """Expose a typed direct-run contract and Apps metadata without profile parameters."""
    scenario = request.getfixturevalue("setup")
    async with Client(server(scenario.service)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    tool = tools["cmake_run_test"]
    assert set(tool.input_schema["properties"]) == {
        "executable",
        "arguments",
        "cwd",
        "timeout",
    }
    assert tool.input_schema["required"] == ["executable"]
    assert tool.input_schema["properties"]["cwd"]["default"] == "project/"
    assert tool.output_schema["properties"]["executable"]["type"] == "string"
    assert tool.icons
    assert tool.meta["ui"]["resourceUri"] == "ui://forgemcp/cmake-run-test.html"
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.idempotent_hint is False


@pytest.mark.anyio
async def test_direct_run_preserves_inputs_and_retains_both_streams(request, monkeypatch):
    """Run a real child with literal arguments, inherited environment, and storage cwd.

    Verify stdin EOF and retain both output streams without copying them into the tool result.
    """
    scenario = request.getfixturevalue("setup")
    executable = request.getfixturevalue("test_program")
    directory = scenario.workspace.storage_root / "test data"
    directory.mkdir(parents=True)
    monkeypatch.setenv("FORGEMCP_DIRECT_TEST", "inherited")
    values = ["a b", "", "a&b|c>d", 'a"b', "%FORGEMCP_DIRECT_TEST%", "Žąsis"]
    arguments = [
        "-c",
        "import json,os,sys; "
        "print(json.dumps([sys.argv[1:], os.getcwd(), "
        "os.environ['FORGEMCP_DIRECT_TEST'], sys.stdin.read()])); "
        "sys.stderr.write('error stream' * 10000)",
        *values,
    ]
    notifications = []

    async def collect(value, total, message):
        """Record progress and require meaningful messages without an invented work total."""
        assert total is None and message
        notifications.append(value)

    try:
        async with Client(server(scenario.service)) as client:
            response = await client.call_tool(
                "cmake_run_test",
                {
                    "executable": executable,
                    "arguments": arguments,
                    "cwd": "storage/test data",
                },
                progress_callback=collect,
            )
        assert not response.is_error
        result = response.structured_content
        assert result["error"] is None
        assert result["executable"] == executable
        assert result["arguments"] == arguments and result["cwd"] == "storage/test data"
        record = scenario.processes.records[result["process_id"]]
        stdout = "".join(entry.text for entry in record.transcript if entry.stream == "stdout")
        assert json.loads(stdout) == [values, str(directory.resolve()), "inherited", ""]
        stderr = "".join(entry.text for entry in record.transcript if entry.stream == "stderr")
        assert stderr.endswith("error stream" * 10000)
        assert record.summary.timeout.total == 600
        assert record.status.current_status == 0
        assert "Succeeded" in response.content[0].text
        assert f"process_get(process_id={result['process_id']})" in response.content[0].text
        assert "error stream" * 2 not in response.content[0].text
        assert notifications[0] == 0 and len(notifications) >= 3
        assert all(right > left for left, right in zip(notifications, notifications[1:]))
        scenario.presets.assert_not_awaited()
        scenario.test.assert_not_awaited()
    finally:
        await scenario.processes.close()


@pytest.mark.anyio
@pytest.mark.parametrize("code", [0, 7])
@pytest.mark.parametrize("arguments", [None, []])
@pytest.mark.parametrize("area", ["project", "storage"])
async def test_direct_run_defaults_and_exit_outcomes(
    request,
    monkeypatch,
    code,
    arguments,
    area,
):
    """Use project cwd and empty arguments by default while reporting raw executable exit codes."""
    scenario = request.getfixturevalue("setup")
    executable = (
        scenario.workspace.root if area == "project" else scenario.workspace.storage_root
    ) / "test-program.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"program")
    session = MagicMock(
        process_id=42,
        close_stdin=AsyncMock(),
        wait=AsyncMock(return_value=code),
    )

    async def output() -> AsyncGenerator[ProcessOutput, None]:
        """Emit no output for a silent executable."""
        for chunk in ():
            yield chunk

    session.output = output
    session.__aenter__.return_value = session
    launch = AsyncMock(return_value=session)
    monkeypatch.setattr(scenario.processes, "launch", launch)
    async with Client(server(scenario.service)) as client:
        response = await client.call_tool(
            "cmake_run_test",
            {"executable": f"{area}/test-program.exe", "arguments": arguments},
        )
    result = response.structured_content
    assert not response.is_error
    assert result["arguments"] == [] and result["cwd"] == "project/"
    assert result["executable"] == f"{area}/test-program.exe"
    assert result["process_id"] == 42
    assert result["error"] == (None if code == 0 else "Test executable exited with 7.")
    assert launch.call_args.args == (executable.resolve(), [])
    assert launch.call_args.kwargs["cwd"] == scenario.workspace.root
    session.close_stdin.assert_awaited_once()
    session.__aexit__.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["total", "idle"])
async def test_direct_run_timeout_retains_process_and_cleans_up(request, mode):
    """Preserve a timed-out child's process reference and terminate it before returning."""
    scenario = request.getfixturevalue("setup")
    executable = request.getfixturevalue("test_program")
    try:
        async with Client(server(scenario.service)) as client:
            response = await client.call_tool(
                "cmake_run_test",
                {
                    "executable": executable,
                    "arguments": ["-c", "import time; time.sleep(20)"],
                    "timeout": {mode: 0.2},
                },
            )
        assert not response.is_error
        result = response.structured_content
        assert result["error"]
        record = scenario.processes.records[result["process_id"]]
        assert record.status.current_status == "interrupted"
        assert not scenario.processes.sessions
    finally:
        await scenario.processes.close()


@pytest.mark.anyio
async def test_direct_run_launch_failure_has_no_process_reference(request):
    """Report a non-executable file as a launch failure without a fictitious process ID."""
    scenario = request.getfixturevalue("setup")
    (scenario.workspace.root / "broken.exe").write_bytes(b"not an executable")
    async with Client(server(scenario.service)) as client:
        response = await client.call_tool("cmake_run_test", {"executable": "broken.exe"})
    assert not response.is_error
    assert response.structured_content["error"]
    assert str(scenario.workspace.root) not in response.structured_content["error"]
    assert response.structured_content["process_id"] is None
    assert "Process: not available" in response.content[0].text
    assert not scenario.processes.records


@pytest.mark.anyio
@pytest.mark.parametrize(
    "inputs",
    [
        {"executable": "missing.exe"},
        {"executable": "project/tests"},
        {"executable": "../outside.exe"},
        {"executable": "root/C:/outside.exe"},
        {"cwd": "missing-directory"},
        {"cwd": "project/test-program.exe"},
        {"cwd": "root/C:/outside"},
        {"arguments": ["before\0after"]},
        {"timeout": {"total": -1}},
    ],
)
async def test_direct_run_rejects_invalid_inputs_before_launch(request, monkeypatch, inputs):
    """Reject unmanaged paths, wrong filesystem kinds, NUL arguments, and invalid timeout values."""
    scenario = request.getfixturevalue("setup")
    (scenario.workspace.root / "test-program.exe").write_bytes(b"program")
    launch = AsyncMock()
    monkeypatch.setattr(scenario.processes, "launch", launch)
    async with Client(server(scenario.service)) as client:
        response = await client.call_tool(
            "cmake_run_test",
            {"executable": "project/test-program.exe", **inputs},
        )
    assert response.is_error
    launch.assert_not_awaited()


@pytest.mark.anyio
async def test_direct_run_cancellation_closes_the_child(request, monkeypatch):
    """Stop a real test child when its MCP request is cancelled."""
    scenario = request.getfixturevalue("setup")
    executable = request.getfixturevalue("test_program")
    started = asyncio.Event()
    children = []
    launch = scenario.processes.launch

    async def capture(*args, **kwargs):
        """Observe the real child so cancellation cleanup can be checked deterministically."""
        session = await launch(*args, **kwargs)
        children.append(session)
        started.set()
        return session

    monkeypatch.setattr(scenario.processes, "launch", capture)
    try:
        async with Client(server(scenario.service)) as client:
            task = asyncio.create_task(
                client.call_tool(
                    "cmake_run_test",
                    {
                        "executable": executable,
                        "arguments": ["-c", "import time; time.sleep(20)"],
                    },
                )
            )
            async with asyncio.timeout(5):
                await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert children[0].returncode is not None
        assert children[0].task.done()
    finally:
        await scenario.processes.close()


@pytest.mark.parametrize("area", ["project", "storage"])
def test_execution_path_rejects_links_outside_the_selected_root(request, monkeypatch, area):
    """Check the resolved target against its selected root even when a lexical path stays inside."""
    scenario = request.getfixturevalue("setup")
    base = scenario.workspace.root if area == "project" else scenario.workspace.storage_root
    path = base / "linked-program"
    resolve = Path.resolve

    def linked_path(value, *args, **kwargs):
        """Simulate an escaping link resolution without requiring Windows symlink privileges."""
        if value == path:
            return base.parent / "external-program"
        return resolve(value, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", linked_path)
    with pytest.raises(CMakeError, match="outside its root"):
        execution_path(
            WorkspacePath(f"{area}/linked-program"),
            scenario.workspace.root,
            scenario.workspace.storage_root,
        )


@pytest.mark.anyio
@pytest.mark.skipif(os.name != "nt", reason="Windows batch execution restriction")
@pytest.mark.parametrize("suffix", [".cmd", ".BAT"])
async def test_direct_run_rejects_windows_batch_files(request, monkeypatch, suffix):
    """Keep Windows batch files out of the native executable launch surface."""
    scenario = request.getfixturevalue("setup")
    (scenario.workspace.root / ("test" + suffix)).write_text("echo test")
    launch = AsyncMock()
    monkeypatch.setattr(scenario.processes, "launch", launch)
    async with Client(server(scenario.service)) as client:
        response = await client.call_tool("cmake_run_test", {"executable": "test" + suffix})
    assert response.is_error
    launch.assert_not_awaited()


@pytest.mark.anyio
async def test_plain_text_reports_profiles_executions_and_all_test_cases(request):
    """Keep preset identity, errors, log references, and every CTest outcome in text results."""
    scenario = request.getfixturevalue("setup")
    scenario.test.return_value = ctest.TestResult(
        return_code=1,
        process_id=42,
        tests=[
            ctest.TestCase(name="good", status="passed", duration_seconds=0.1),
            ctest.TestCase(name="broken", status="failed", message="expected 2\nreceived 3"),
            ctest.TestCase(name="optional", status="skipped", message="not supported"),
            ctest.TestCase(name="disabled", status="not_run"),
        ],
    )
    scenario.build.return_value = cmake.BuildResult(process_id=41)
    async with Client(server(scenario.service)) as client:
        profiles = await client.call_tool("cmake_profiles", {})
        assert "Configure presets: native" in profiles.content[0].text
        assert profiles.structured_content["result"][0]["name"] == "presets"
        configured = await client.call_tool("cmake_configure", {})
        assert "presets (preset: native): Succeeded" in configured.content[0].text
        assert "process_get(process_id=40)" in configured.content[0].text
        built = await client.call_tool("cmake_build", {})
        assert "Unknown completed; unknown total" in built.content[0].text
        assert built.structured_content["result"][0]["completed_steps"] is None
        tested = await client.call_tool("cmake_test", {})
        text = tested.content[0].text
        assert not tested.is_error
        assert tested.structured_content["result"][0]["error"] is not None
        assert "0 executions succeeded; 1 failed" in text
        assert "1 passed; 1 failed; 1 skipped; 1 not run" in text
        for name in ("good", "broken", "optional", "disabled"):
            assert name in text
        assert "expected 2\n      received 3" in text
        assert "process_get(process_id=42)" in text
        assert "```" not in text


def test_profile_options_keep_operator_choices():
    """Verify CLI profile parsing preserves exact operator settings and cache values."""
    profile, = parse_profiles(
        [
            [
                "cross",
                "toolset=custom",
                "configuration=Debug",
                "define=MODE=a=b",
                "toolchain-file=project/cross.cmake",
                "build-directory=storage/cross",
            ],
        ],
    )
    assert profile.toolset == "custom"
    assert profile.definitions == {"MODE": "a=b"}
    assert profile.toolchain_file == WorkspacePath("project/cross.cmake")
    assert profile.build_directory == WorkspacePath("storage/cross")


@pytest.mark.parametrize(
    "groups",
    [
        [["same"], ["same"]],
        [["x", "generator=Ninja", "generator=Other"]],
        [["x", "configure-preset=native", "generator=Ninja"]],
        [["x", "define=A=1", "define=A=2"]],
    ],
)
def test_conflicting_profiles_rejected(groups):
    """Verify incompatible manual and preset settings raise a CMake domain error."""
    with pytest.raises(CMakeError):
        parse_profiles(groups)


@pytest.mark.anyio
async def test_automatic_native_presets_and_plain_defaults(setup):
    """Verify automatic profiles select native presets or separate Debug and Release builds."""
    definitions, catalogs = await setup.service.selection(None)
    assert definitions[0].configure_presets == ["native"]
    profile = setup.service.resolve_profile(definitions[0], catalogs)
    assert profile.build_directory is None
    assert setup.service.operation_presets(profile, "build") == ["native-build"]
    for filename in ("CMakePresets.json", "CMakeUserPresets.json"):
        (setup.workspace.root / filename).unlink(missing_ok=True)
    definitions, _ = await setup.service.selection(None)
    assert [item.name for item in definitions] == ["Debug", "Release"]
    assert setup.service.plain_profile(definitions[0]).generator == "Ninja"
    assert setup.service.configure_definitions(definitions[0])["CMAKE_EXPORT_COMPILE_COMMANDS"] == "ON"
    with pytest.raises(CMakeError, match="Unknown"):
        await setup.service.selection(["missing"])


@pytest.mark.anyio
@pytest.mark.parametrize("blocked", [None, "protected", "foreign"])
async def test_generator_change_cleans_only_eligible_build_directory(setup, blocked):
    """Verify generator replacement cleans owned build trees and rejects unsafe targets."""
    setup.service.definitions = (ProfileDefinition(name="debug", configuration="Debug"),)
    directory = setup.workspace.storage_root / "build/cmake-debug"
    (directory / "nested").mkdir(parents=True)
    stale = directory / "nested/old.obj"
    stale.write_text("stale")
    source = setup.workspace.root if blocked != "foreign" else setup.workspace.root / "other"
    (directory / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={source.as_posix()}\nCMAKE_GENERATOR:INTERNAL=Visual Studio\n"
    )
    if blocked == "protected":
        setup.workspace.protect_path(WorkspacePath("storage/build/cmake-debug/nested/old.obj"))

    async def configure(*args, **kwargs):
        """Simulate successful configuration by writing the expected cache in the isolated build
        tree.
        """
        assert not stale.exists()
        assert (directory / ".cmake/api/v1/query/client-forgemcp/codemodel-v2").is_file()
        assert kwargs["generator"] == "Ninja"
        write_generation(setup)
        return cmake.ConfigureResult(process_id=40)

    setup.configure.side_effect = configure
    async with Client(server(setup.service)) as client:
        response = await client.call_tool("cmake_configure", {})
    assert not response.is_error
    result = response.structured_content["result"][0]
    if blocked:
        assert result["error"]
        assert stale.exists()
        setup.configure.assert_not_awaited()
    else:
        assert result["error"] is None
        assert result["compilation_database"] == "storage/build/cmake-debug/compile_commands.json"
        assert not {"status", "return_code", "tests"}.intersection(result)


@pytest.mark.anyio
async def test_native_batch_continues_after_failure_and_tools_have_distinct_results(setup):
    """Verify a failed preset does not stop the batch and each tool returns its own result shape."""
    setup.service.definitions = tuple(
        ProfileDefinition(
            name=name,
            configure_presets=["native"],
            build_presets=["native-build"],
            test_presets=["native-test"],
        )
        for name in ("first", "second")
    )
    setup.build.side_effect = [
        cmake.BuildResult(return_code=1, process_id=41),
        cmake.BuildResult(completed_steps=3, total_steps=3),
    ]
    setup.test.return_value = ctest.TestResult(
        return_code=0,
        process_id=42,
        tests=[ctest.TestCase(name="broken", status="failed")],
    )
    async with Client(server(setup.service)) as client:
        configured = await client.call_tool("cmake_configure", {})
        built = await client.call_tool("cmake_build", {})
        tested = await client.call_tool("cmake_test", {"profiles": ["first"]})
    assert not configured.is_error and not built.is_error and not tested.is_error
    assert [item["process_id"] for item in configured.structured_content["result"]] == [40, 40]
    setup.configure.assert_awaited_once()
    assert setup.configure.call_args.args == (setup.workspace.root, None)
    assert setup.configure.call_args.kwargs["generator"] is None
    results = built.structured_content["result"]
    assert results[0]["error"] and results[1]["error"] is None
    assert results[0]["process_id"] == 41
    assert results[1]["completed_steps"] == 3
    assert "tests" not in results[0]
    test_result = tested.structured_content["result"][0]
    assert test_result["process_id"] == 42
    assert "output_tail" not in test_result
    assert test_result["error"]
    assert test_result["tests"][0]["name"] == "broken"
    assert "completed_steps" not in test_result
    assert not setup.test.call_args.kwargs["report_path"].parent.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["configure", "build", "test"])
async def test_command_errors_keep_log_reference_in_tool_result(setup, operation):
    """Verify failed commands preserve their process ID in the model-visible result."""
    command = getattr(setup, operation)
    command.side_effect = ToolCommandError("Timed out", process_id=73)
    async with Client(server(setup.service)) as client:
        response = await client.call_tool(f"cmake_{operation}", {})
    assert not response.is_error
    result = response.structured_content["result"][0]
    assert result["process_id"] == 73
    assert result["error"] == "Timed out"
    assert "output_tail" not in result


@pytest.mark.anyio
async def test_subscription_reports_successful_configs_and_removes_failed_or_missing_databases(setup):
    """Verify subscriptions retain successful databases and remove failed or vanished
    configurations.
    """
    setup.service.definitions = (ProfileDefinition(name="debug", configuration="Debug"),)
    updates = setup.service.configuration_updates()
    assert await anext(updates) == []
    directory = setup.workspace.storage_root / "build/cmake-debug"

    async def configure(*args, **kwargs):
        """Create a scripted compilation database or return a failed configure outcome."""
        write_generation(setup)
        return cmake.ConfigureResult(process_id=40)

    setup.configure.side_effect = configure
    try:
        async with Client(server(setup.service)) as client:
            await client.call_tool("cmake_configure", {})
            snapshot = await asyncio.wait_for(anext(updates), timeout=5)
            assert len(snapshot) == 1
            assert snapshot[0].id == "debug"
            assert snapshot[0].build_directory == WorkspacePath("storage/build/cmake-debug")
            assert snapshot[0].compilation_database == WorkspacePath(
                "storage/build/cmake-debug/compile_commands.json",
            )
            snapshot[0].id = "caller changed its copy"
            assert (await setup.service.compilation_contexts())[0].id == "debug"

            setup.configure.side_effect = None
            setup.configure.return_value = cmake.ConfigureResult(return_code=1)
            failed = await client.call_tool("cmake_configure", {})
            assert failed.structured_content["result"][0]["error"]
            assert await asyncio.wait_for(anext(updates), timeout=5) == []

            setup.configure.side_effect = configure
            await client.call_tool("cmake_configure", {})
            assert len(await asyncio.wait_for(anext(updates), timeout=5)) == 1
            (directory / "compile_commands.json").unlink()
            await setup.service.publish_configurations()
            assert await asyncio.wait_for(anext(updates), timeout=5) == []
    finally:
        await updates.aclose()
    assert not setup.service.configuration_subscribers
