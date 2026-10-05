"""Operator choices and CMake orchestration, without host tool discovery."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.apps import Apps

from forgemcp.cmake.errors import CMakeError
from forgemcp.cmake.profiles import ProfileDefinition, parse_profiles
from forgemcp.cmake.service import CMakeService
from forgemcp.completion import Complete
from forgemcp.toolchain.errors import ToolCommandError
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.spec import ToolKind, Toolset, ToolSpec
from forgemcp.toolchain.tools import cmake, ctest
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import WorkspaceService


@pytest.fixture
def anyio_backend():
    """Run async tests on the asyncio backend used by the process and LSP services."""
    return "asyncio"


@pytest.fixture
def setup(cpp_acceptance_project):
    """Create an isolated CMake service with scripted tool operations and captured invocations."""
    workspace = WorkspaceService(cpp_acceptance_project)
    configure = AsyncMock(return_value=cmake.ConfigureResult(process_id=40))
    build = AsyncMock(return_value=cmake.BuildResult(completed_steps=2, total_steps=2))
    test = AsyncMock(return_value=ctest.TestResult(return_code=0, process_id=42))
    presets = AsyncMock(
        return_value=cmake.PresetsResult(
            configure=["native"],
            build=["native-build"],
            test=["native-test"],
        ),
    )
    tools = (
        ToolSpec(
            "cmake",
            ToolKind.BUILD_SYSTEM,
            workspace.root / "cmake",
            {"configure": configure, "build": build, "presets": presets},
        ),
        ToolSpec(
            "ctest",
            ToolKind.TEST_RUNNER,
            workspace.root / "ctest",
            {"test": test},
        ),
        ToolSpec(
            "ninja",
            ToolKind.BUILD_RUNNER,
            workspace.root / "ninja",
            {},
        ),
    )
    toolchains = ToolchainService(SimpleNamespace())
    toolchains.toolsets = (Toolset("system", "System", tools, None, True),)
    service = CMakeService(
        toolchains,
        project_root=workspace.root,
        storage_root=workspace.storage_root,
        protected_paths=workspace.protected_paths,
        progress_interval=0,
    )
    return SimpleNamespace(
        workspace=workspace,
        service=service,
        configure=configure,
        build=build,
        test=test,
        presets=presets,
    )


def server(service):
    """Mount the service's Apps bindings on an in-process MCP server."""
    apps = Apps()
    mcp = MCPServer("cmake-unit", extensions=[apps])
    service.register(mcp, apps, Complete())
    for binding in apps.tools():
        mcp.add_tool(binding.fn, meta=binding.meta, **binding.kwargs)
    for binding in apps.resources():
        mcp.add_resource(binding.resource)
    return mcp


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
        (directory / "compile_commands.json").write_text("[]")
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
        (directory / "compile_commands.json").write_text("[]")
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
