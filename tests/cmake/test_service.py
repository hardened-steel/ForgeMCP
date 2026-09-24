"""Operator choices and CMake orchestration, without host tool discovery."""
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
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.spec import ToolKind, Toolset, ToolSpec
from forgemcp.toolchain.tools import cmake, ctest
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import WorkspaceService


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def setup(cpp_acceptance_project):
    workspace = WorkspaceService(cpp_acceptance_project)
    configure = AsyncMock(return_value=cmake.ConfigureResult())
    build = AsyncMock(return_value=cmake.BuildResult(completed_steps=2, total_steps=2))
    test = AsyncMock(return_value=ctest.TestResult(return_code=0, output_tail=""))
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
    service = CMakeService(workspace, toolchains, progress_interval=0)
    return SimpleNamespace(
        workspace=workspace,
        service=service,
        configure=configure,
        build=build,
        test=test,
        presets=presets,
    )


def server(service):
    mcp = MCPServer("cmake-unit")
    service.register(mcp, Apps(), Complete())
    return mcp


def test_profile_options_keep_operator_choices():
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
    with pytest.raises(CMakeError):
        parse_profiles(groups)


@pytest.mark.anyio
async def test_automatic_native_presets_and_plain_defaults(setup):
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
    setup.service.definitions = (ProfileDefinition(name="debug", configuration="Debug"),)
    directory = setup.workspace.storage_directory("build").subdirectory("cmake-debug").path
    (directory / "nested").mkdir()
    stale = directory / "nested/old.obj"
    stale.write_text("stale")
    source = setup.workspace.root if blocked != "foreign" else setup.workspace.root / "other"
    (directory / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={source.as_posix()}\nCMAKE_GENERATOR:INTERNAL=Visual Studio\n"
    )
    if blocked == "protected":
        setup.workspace.protect_path(WorkspacePath("storage/build/cmake-debug/nested/old.obj"))

    async def configure(*args, **kwargs):
        assert not stale.exists()
        assert (directory / ".cmake/api/v1/query/client-forgemcp/codemodel-v2").is_file()
        assert kwargs["generator"] == "Ninja"
        (directory / "compile_commands.json").write_text("[]")
        return cmake.ConfigureResult()

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
        cmake.BuildResult(return_code=1),
        cmake.BuildResult(completed_steps=3, total_steps=3),
    ]
    setup.test.return_value = ctest.TestResult(
        return_code=0,
        output_tail="",
        tests=[ctest.TestCase(name="broken", status="failed")],
    )
    async with Client(server(setup.service)) as client:
        configured = await client.call_tool("cmake_configure", {})
        built = await client.call_tool("cmake_build", {})
        tested = await client.call_tool("cmake_test", {"profiles": ["first"]})
    assert not configured.is_error and not built.is_error and not tested.is_error
    setup.configure.assert_awaited_once()
    assert setup.configure.call_args.args == (setup.workspace.root, None)
    assert setup.configure.call_args.kwargs["generator"] is None
    results = built.structured_content["result"]
    assert results[0]["error"] and results[1]["error"] is None
    assert results[1]["completed_steps"] == 3
    assert "tests" not in results[0]
    test_result = tested.structured_content["result"][0]
    assert test_result["error"]
    assert test_result["tests"][0]["name"] == "broken"
    assert "completed_steps" not in test_result
    assert not setup.test.call_args.kwargs["report_path"].parent.exists()
