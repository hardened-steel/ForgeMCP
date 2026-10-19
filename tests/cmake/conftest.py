"""Shared isolated CMake operations and scripted File API generations."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server import MCPServer
from mcp.server.apps import Apps

from forgemcp.cmake.service import CMakeService
from forgemcp.cmake.workspace import CMakeProvider
from forgemcp.completion import Complete
from forgemcp.process.service import ProcessService
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.spec import ToolKind, Toolset, ToolSpec
from forgemcp.toolchain.tools import cmake, ctest
from forgemcp.workspace.service import WorkspaceService


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
    processes = ProcessService(workspace.root, allowed_roots=(workspace.storage_root,))
    service = CMakeService(
        toolchains,
        processes,
        project_root=workspace.root,
        storage_root=workspace.storage_root,
        protected_paths=workspace.protected_paths,
        progress_interval=0,
    )
    return SimpleNamespace(
        workspace=workspace,
        service=service,
        processes=processes,
        configure=configure,
        build=build,
        test=test,
        presets=presets,
    )


def server(service, workspace=None):
    """Mount the service's Apps bindings on an in-process MCP server."""
    apps = Apps()
    mcp = MCPServer("cmake-unit", extensions=[apps])
    complete = Complete()
    service.register(mcp, apps, complete)
    if workspace is not None:
        workspace.register_provider(
            "cmake",
            CMakeProvider(service),
            tools=CMakeProvider.TOOLS,
        )
        workspace.register(mcp, apps, complete)
    for binding in apps.tools():
        mcp.add_tool(binding.fn, meta=binding.meta, **binding.kwargs)
    for binding in apps.resources():
        mcp.add_resource(binding.resource)
    return mcp


def write_generation(scenario, name="debug", inputs=None, minor=1, globs=()):
    """Write a new successful File API generation and its cache in the isolated build tree."""
    directory = scenario.workspace.storage_root / f"build/cmake-{name}"
    reply = directory / ".cmake/api/v1/reply"
    reply.mkdir(parents=True, exist_ok=True)
    number = len(list(reply.glob("index-*.json"))) + 1
    filename = f"cmakeFiles-{number}.json"
    (reply / filename).write_text(
        json.dumps(
            {
                "kind": "cmakeFiles",
                "version": {"major": 1, "minor": minor},
                "paths": {
                    "source": str(scenario.workspace.root),
                    "build": str(directory),
                },
                "inputs": [{"path": path} for path in (inputs or ["CMakeLists.txt"])],
                "globsDependent": list(globs),
            }
        ),
        encoding="utf-8",
    )
    (reply / f"index-{number:06}.json").write_text(
        json.dumps(
            {"reply": {"client-forgemcp": {"cmakeFiles-v1": {"jsonFile": filename}}}}
        ),
        encoding="utf-8",
    )
    (directory / "compile_commands.json").write_text("[]", encoding="utf-8")
    (directory / "CMakeCache.txt").write_text(
        f"CMAKE_HOME_DIRECTORY:INTERNAL={scenario.workspace.root.as_posix()}\n"
        "CMAKE_GENERATOR:INTERNAL=Ninja\nCMAKE_BUILD_TYPE:STRING=Debug\n",
        encoding="utf-8",
    )
    return directory
