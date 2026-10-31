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


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("cmake_configure", {"profiles": []}, ["profiles=[]", "null", "presets"]),
        (
            "cmake_build",
            {"profiles": ["presets", "presets"]},
            ["profiles[1]", "profiles[0]", "duplicates"],
        ),
        (
            "cmake_test",
            {"profiles": ["typo"]},
            ["profiles[0]", "typo", "presets", "cmake_profiles()"],
        ),
        ("cmake_build", {"targets": []}, ["targets=[]", "null", "defaults"]),
        ("cmake_build", {"targets": ["valid", ""]}, ["targets[1]", "nonempty"]),
        ("cmake_test", {"names": [""]}, ["names[0]", "exact test name"]),
    ],
)
async def test_invalid_selections_identify_argument_and_recovery(
    request,
    tool,
    arguments,
    expected,
):
    """Reject invalid selections with a precise field and usable selection guidance."""
    scenario = request.getfixturevalue("setup")
    async with Client(server(scenario.service)) as client:
        response = await client.call_tool(tool, arguments)
    assert response.is_error
    text = response.content[0].text
    assert all(value in text for value in expected)
    scenario.configure.assert_not_awaited()
    scenario.build.assert_not_awaited()
    scenario.test.assert_not_awaited()


@pytest.mark.anyio
@pytest.mark.parametrize("parameter", ["executable", "cwd"])
async def test_external_execution_error_names_path_parameter(request, parameter):
    """Identify which execution path violates the managed-root requirement."""
    scenario = request.getfixturevalue("setup")
    (scenario.workspace.root / "program").write_bytes(b"program")
    arguments = {"executable": "project/program", parameter: "root/C:/outside"}
    async with Client(server(scenario.service)) as client:
        response = await client.call_tool("cmake_run_test", arguments)
    assert response.is_error
    assert parameter + "=" in response.content[0].text
    assert "project/ or storage/" in response.content[0].text


@pytest.mark.anyio
async def test_direct_run_nul_error_names_argument_index(request):
    """Report the invalid argument index without echoing other command arguments."""
    scenario = request.getfixturevalue("setup")
    async with Client(server(scenario.service)) as client:
        response = await client.call_tool(
            "cmake_run_test",
            {"executable": "program", "arguments": ["private-value", "bad\0argument"]},
        )
    assert response.is_error
    assert "arguments[1]" in response.content[0].text
    assert "private-value" not in response.content[0].text


def test_unusable_cache_preserves_cause_and_profile_recovery(request):
    """Keep the cache failure and an exact configure call without exposing native roots."""
    scenario = request.getfixturevalue("setup")
    profile = scenario.service.plain_profile(ProfileDefinition(name="debug"))
    with pytest.raises(CMakeError) as failure:
        scenario.service.require_configured(profile)
    text = str(failure.value)
    assert "debug" in text and "CMakeCache.txt" in text
    assert "storage/build/cmake-debug" in text
    assert "cmake_configure(profiles=['debug'])" in text
    assert str(scenario.workspace.root) not in text


@pytest.mark.anyio
async def test_preset_failure_lists_available_names(request):
    """Use the already acquired preset catalog to explain a bad operator selection."""
    scenario = request.getfixturevalue("setup")
    scenario.service.definitions = (
        ProfileDefinition(name="custom", configure_presets=["typo"]),
    )
    async with Client(server(scenario.service)) as client:
        response = await client.call_tool("cmake_configure", {})
    assert not response.is_error
    error = response.structured_content["result"][0]["error"]
    assert all(value in error for value in ("custom", "typo", "native", "operator"))
    scenario.presets.assert_awaited_once()
    scenario.configure.assert_not_awaited()


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


@pytest.mark.anyio
async def test_startup_configures_once_without_blocking_mcp_or_restarting_for_another_client(
    setup,
    runtime,
):
    """Serve requests during startup configure and share one batch across lifespan users."""
    started = asyncio.Event()
    resume = asyncio.Event()

    async def configure(_source, build_directory, **_kwargs):
        """Pause after the clangd subscription exists, then confirm each selected profile."""
        assert runtime.analysis.configuration_task is not None
        assert runtime.builds.configuration_subscribers
        started.set()
        await resume.wait()
        write_generation(setup, name=build_directory.name.removeprefix("cmake-"))
        return cmake.ConfigureResult(process_id=40)

    setup.configure.side_effect = configure
    async with asyncio.timeout(5), runtime.lifespan():
        async with Client(runtime.mcp) as client:
            await started.wait()
            assert runtime.users == 2
            response = await client.call_tool("cmake_profiles", {})
            assert not response.is_error
            assert (await runtime.state_snapshot())["cmake"]["startup_configure"]["running"]
            resume.set()
            await runtime.startup_configure_task
            state = (await runtime.state_snapshot())["cmake"]["startup_configure"]
            assert not state["running"] and state["error"] is None
            assert [result["profile"] for result in state["results"]] == ["debug", "release"]
            assert all(result["error"] is None for result in state["results"])
        assert not runtime.closed
        assert setup.configure.await_count == 2
    assert runtime.closed and runtime.analysis.closed


@pytest.mark.anyio
async def test_startup_continues_after_profile_failure_and_retains_outcomes(setup, runtime, caplog):
    """Publish successful contexts and retain failed executions without stopping the server."""
    async def configure(_source, build_directory, **_kwargs):
        """Fail debug while producing a usable release generation."""
        if build_directory.name == "cmake-debug":
            return cmake.ConfigureResult(return_code=1, process_id=41)
        write_generation(setup, name="release")
        return cmake.ConfigureResult(process_id=42)

    setup.configure.side_effect = configure
    with caplog.at_level("INFO", logger="forgemcp.server"):
        async with asyncio.timeout(5), Client(runtime.mcp) as client:
            await runtime.startup_configure_task
            assert not (await client.call_tool("workspace_list", {})).is_error
            state = (await runtime.state_snapshot())["cmake"]["startup_configure"]
            assert state["results"][0]["error"] == "CMake configure exited with 1."
            assert state["results"][0]["process_id"] == 41
            assert state["results"][1]["error"] is None
            assert state["results"][1]["process_id"] == 42
            assert [context.id for context in await runtime.builds.compilation_contexts()] == [
                "release",
            ]
    assert "2 executions, 1 failures" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        ToolCommandError("private command details"),
        RuntimeError("private command details"),
    ],
)
async def test_startup_batch_failure_keeps_server_available_and_sanitizes_logs(
    runtime,
    monkeypatch,
    caplog,
    failure,
):
    """Retain expected selection errors and sanitize unexpected background failures."""
    monkeypatch.setattr(runtime.builds, "selection", AsyncMock(side_effect=failure))
    async with asyncio.timeout(5), Client(runtime.mcp) as client:
        await asyncio.gather(runtime.startup_configure_task, return_exceptions=True)
        assert not (await client.call_tool("workspace_list", {})).is_error
        state = (await runtime.state_snapshot())["cmake"]["startup_configure"]
        assert state["results"] == [] and not state["running"]
        assert state["error"] == (
            str(failure)
            if isinstance(failure, ToolCommandError)
            else "Startup CMake configuration failed unexpectedly."
        )
    assert "private command details" not in caplog.text


@pytest.mark.anyio
async def test_shutdown_cancels_startup_before_closing_clangd_and_releases_command_lock(
    setup,
    runtime,
):
    """Cancel an active configure before its subscriber and process service are closed."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def configure(*_args, **_kwargs):
        """Wait for shutdown and verify clangd is alive while configure unwinds."""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            assert not runtime.analysis.closed
            cancelled.set()

    setup.configure.side_effect = configure
    async with asyncio.timeout(5):
        async with runtime.lifespan():
            await started.wait()
            assert runtime.builds.operation_lock.locked()
    assert cancelled.is_set() and runtime.startup_configure_task.cancelled()
    assert not runtime.builds.operation_lock.locked()
    assert not runtime.builds.configuration_subscribers
    assert await runtime.builds.compilation_contexts() == []
    assert runtime.analysis.closed


@pytest.mark.anyio
async def test_startup_publishes_context_before_later_profiles_finish(setup, runtime):
    """Notify subscribers after each successful configure while the startup batch is running."""
    release_started = asyncio.Event()
    resume = asyncio.Event()

    async def configure(_source, build_directory, **_kwargs):
        """Confirm debug and pause release so the first context can be observed independently."""
        name = build_directory.name.removeprefix("cmake-")
        if name == "release":
            release_started.set()
            await resume.wait()
        write_generation(setup, name=name)
        return cmake.ConfigureResult()

    setup.configure.side_effect = configure
    updates = runtime.builds.configuration_updates()
    assert await anext(updates) == []
    try:
        async with asyncio.timeout(5), runtime.lifespan():
            await release_started.wait()
            assert not runtime.startup_configure_task.done()
            assert [context.id for context in await anext(updates)] == ["debug"]
            resume.set()
            await runtime.startup_configure_task
            assert [context.id for context in await anext(updates)] == ["debug", "release"]
    finally:
        await updates.aclose()


@pytest.mark.anyio
async def test_explicit_configure_waits_for_active_startup_command(setup, runtime, monkeypatch):
    """Serialize MCP configure with startup through the shared CMake operation lock."""
    started = asyncio.Event()
    manual_selected = asyncio.Event()
    resume = asyncio.Event()
    selection = runtime.builds.selection

    async def select(profiles):
        """Expose when the explicit request has selected its profiles and will acquire the lock."""
        result = await selection(profiles)
        if profiles is not None:
            manual_selected.set()
        return result

    async def configure(_source, build_directory, **_kwargs):
        """Hold the active command until the explicit request has reached the same lock."""
        started.set()
        await resume.wait()
        write_generation(setup, name=build_directory.name.removeprefix("cmake-"))
        return cmake.ConfigureResult()

    monkeypatch.setattr(runtime.builds, "selection", select)
    setup.configure.side_effect = configure
    async with asyncio.timeout(5), Client(runtime.mcp) as client:
        await started.wait()
        manual = asyncio.create_task(client.call_tool("cmake_configure", {"profiles": ["debug"]}))
        await manual_selected.wait()
        assert setup.configure.await_count == 1 and not manual.done()
        resume.set()
        assert not (await manual).is_error
        await runtime.startup_configure_task
        assert setup.configure.await_count == 3
