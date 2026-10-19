"""CMake input freshness, controlled mutations, and generation evidence."""

import asyncio

import pytest
from mcp import Client

from forgemcp.cmake.file_api import generation, read_generation
from forgemcp.cmake.profiles import ProfileDefinition
from forgemcp.cmake.workspace import CMakeProvider
from forgemcp.toolchain.tools import cmake
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import ResultProvider

from tests.cmake.conftest import server, write_generation


@pytest.mark.anyio
async def test_file_api_refresh_replaces_inputs_and_excludes_external_files(request):
    """Replace each tree's dependency set without reading external modules or retaining contents."""
    scenario = request.getfixturevalue("setup")
    root = scenario.workspace.root
    extra = root / "extra.cmake"
    extra.write_text("set(OPTION ON)")
    external = root.parent / "external.cmake"
    external.write_text("untrusted external data")
    stored = scenario.workspace.storage_root / "settings.cmake"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text("set(STORED ON)")
    directory = WorkspacePath("storage/build/cmake-debug")
    write_generation(
        scenario,
        inputs=["CMakeLists.txt", "extra.cmake", str(stored), str(external)],
    )
    native = scenario.service.build_path(directory)
    snapshot = read_generation(native, None, root, scenario.workspace.storage_root)
    assert snapshot is not None
    scenario.service.inputs[directory] = snapshot
    assert set(snapshot.checksums) == {
        WorkspacePath("project/CMakeLists.txt"),
        WorkspacePath("project/extra.cmake"),
        WorkspacePath("storage/settings.cmake"),
        WorkspacePath("project/CMakePresets.json"),
        WorkspacePath("project/CMakeUserPresets.json"),
    }
    assert len(snapshot.checksums[WorkspacePath("project/extra.cmake")]) == 64
    previous = generation(scenario.service.build_path(directory))
    write_generation(scenario, inputs=["CMakeLists.txt"])
    updated = read_generation(native, previous, root, scenario.workspace.storage_root)
    assert updated is not None
    scenario.service.inputs[directory] = updated
    assert (
        WorkspacePath("project/extra.cmake")
        not in scenario.service.inputs[directory].checksums
    )
    assert (
        WorkspacePath("storage/settings.cmake")
        not in scenario.service.inputs[directory].checksums
    )
    assert (
        read_generation(
            native,
            generation(native),
            root,
            scenario.workspace.storage_root,
        )
        is None
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            "workspace_write_file",
            {"path": "project/CMakeLists.txt", "text": "# replaced"},
        ),
        (
            "workspace_edit_file",
            {
                "path": "project/CMakeLists.txt",
                "old_text": "project(",
                "new_text": "# changed\nproject(",
            },
        ),
        ("workspace_delete", {"path": "project/CMakeLists.txt"}),
        (
            "workspace_move",
            {"source": "project/CMakeLists.txt", "destination": "project/moved.cmake"},
        ),
        (
            "workspace_move",
            {"source": "project/include", "destination": "project/moved-include"},
        ),
        ("workspace_write_file", {"path": "storage/input.cmake", "text": "changed"}),
        ("workspace_write_file", {"path": "project/CMakePresets.json", "text": "{}"}),
    ],
)
async def test_workspace_mutations_withdraw_all_shared_contexts(
    request,
    tool,
    arguments,
):
    """Invalidate every consumer of a changed input, including nested moves and storage inputs."""
    scenario = request.getfixturevalue("setup")
    stored = scenario.workspace.storage_root / "input.cmake"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text("original")
    scenario.service.definitions = tuple(
        (
            ProfileDefinition(name=name) for name in ("debug", "release")
        ),
    )

    async def configure(_source, build_directory, **_kwargs):
        """Generate two independent trees with a shared set of configuration inputs."""
        name = build_directory.name.removeprefix("cmake-")
        write_generation(
            scenario,
            name,
            ["CMakeLists.txt", "include/fixture/math.hpp", str(stored)],
        )
        return cmake.ConfigureResult()

    scenario.configure.side_effect = configure
    updates = scenario.service.configuration_updates()
    assert await anext(updates) == []
    try:
        async with Client(server(scenario.service, scenario.workspace)) as client:
            await client.call_tool("cmake_configure", {})
            snapshot = await anext(updates)
            assert len(snapshot) == 2
            response = await client.call_tool(tool, arguments)
            assert not response.is_error
            assert await anext(updates) == []
            assert await scenario.service.compilation_contexts() == []
            assert len(snapshot) == 2
            for name in ("debug", "release"):
                database = (
                    scenario.workspace.storage_root
                    / f"build/cmake-{name}/compile_commands.json"
                )
                assert database.exists()
            await scenario.service.publish_configurations(refresh=True)
            assert await anext(updates) == []
            assert not scenario.service.operation_lock.locked()
    finally:
        await updates.aclose()


@pytest.mark.anyio
async def test_identical_content_and_normal_source_edit_keep_context(request):
    """Ignore timestamp-only changes and ordinary source edits."""
    scenario = request.getfixturevalue("setup")
    scenario.service.definitions = (ProfileDefinition(name="debug"),)

    async def configure(*_args, **_kwargs):
        """Write a confirmed tree with only CMake inputs."""
        write_generation(scenario)
        return cmake.ConfigureResult()

    scenario.configure.side_effect = configure
    async with Client(server(scenario.service, scenario.workspace)) as client:
        await client.call_tool("cmake_configure", {})
        checksums = dict(
            scenario.service.inputs[
                WorkspacePath("storage/build/cmake-debug")
            ].checksums
        )
        for path in ("CMakeLists.txt", "src/math.cpp"):
            text = (scenario.workspace.root / path).read_bytes().decode("utf-8")
            response = await client.call_tool(
                "workspace_write_file",
                {"path": path, "text": text},
            )
            assert not response.is_error
        response = await client.call_tool(
            "workspace_write_file",
            {"path": "src/math.cpp", "text": "// ordinary source change"},
        )
        assert not response.is_error
        assert len(await scenario.service.compilation_contexts()) == 1
        assert (
            scenario.service.inputs[
                WorkspacePath("storage/build/cmake-debug")
            ].checksums
            == checksums
        )
        scenario.configure.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "glob_policy",
    [(1, [], False), (1, [{}], True), (0, [], True)],
)
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("workspace_write_file", {"path": "project/new.cpp", "text": "// new source"}),
        ("workspace_delete", {"path": "project/src/math.cpp"}),
        ("workspace_move", {"source": "project/src", "destination": "project/new-src"}),
        ("workspace_mkdir", {"path": "project/new-directory"}),
    ],
)
async def test_structural_glob_invalidation_is_conservative(
    request,
    glob_policy,
    tool,
    arguments,
):
    """Use File API glob availability and never implement CMake's glob matching engine."""
    minor, globs, invalidated = glob_policy
    scenario = request.getfixturevalue("setup")
    scenario.service.definitions = (ProfileDefinition(name="debug"),)

    async def configure(*_args, **_kwargs):
        """Generate a reply with or without structural dependency information."""
        write_generation(scenario, minor=minor, globs=globs)
        return cmake.ConfigureResult()

    scenario.configure.side_effect = configure
    async with Client(server(scenario.service, scenario.workspace)) as client:
        await client.call_tool("cmake_configure", {})
        response = await client.call_tool(tool, arguments)
        assert not response.is_error
        assert bool(await scenario.service.compilation_contexts()) is not invalidated


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("regenerate", "return_code"),
    [(True, 0), (True, 1), (False, 0), (False, 1)],
)
async def test_build_restores_only_a_new_generation_even_if_compilation_fails(
    request,
    regenerate,
    return_code,
):
    """Restore contexts from generation even when subsequent compilation fails."""
    scenario = request.getfixturevalue("setup")
    scenario.service.definitions = (ProfileDefinition(name="debug"),)
    directory = WorkspacePath("storage/build/cmake-debug")

    async def configure(*_args, **_kwargs):
        """Write a successful configure generation."""
        write_generation(scenario)
        return cmake.ConfigureResult()

    async def build(*_args, **_kwargs):
        """Optionally regenerate before returning a compilation outcome."""
        if regenerate:
            write_generation(scenario, inputs=["CMakeLists.txt", "README.md"])
        return cmake.BuildResult(return_code=return_code)

    scenario.configure.side_effect = configure
    scenario.build.side_effect = build
    async with Client(server(scenario.service, scenario.workspace)) as client:
        await client.call_tool("cmake_configure", {})
        baseline = dict(scenario.service.inputs[directory].checksums)
        await client.call_tool(
            "workspace_write_file",
            {"path": "CMakeLists.txt", "text": "# changed"},
        )
        assert await scenario.service.compilation_contexts() == []
        response = await client.call_tool("cmake_build", {})
        assert not response.is_error
        assert bool(response.structured_content["result"][0]["error"]) == bool(
            return_code
        )
        assert bool(await scenario.service.compilation_contexts()) == regenerate
        if regenerate:
            assert (
                WorkspacePath("project/README.md")
                in scenario.service.inputs[directory].checksums
            )
        else:
            assert scenario.service.inputs[directory].checksums == baseline
        scenario.build.assert_awaited_once()
        assert not scenario.service.operation_lock.locked()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    ["configure", "old_reply", "build_error_reply", "invalid_reply"],
)
async def test_failed_or_unconfirmed_generation_cannot_reactivate_artifacts(
    request, failure
):
    """Exclude old artifacts after failed or unconfirmed generation."""
    scenario = request.getfixturevalue("setup")
    scenario.service.definitions = (ProfileDefinition(name="debug"),)

    async def configure(*_args, **_kwargs):
        """Provide a valid initial configure."""
        write_generation(scenario)
        return cmake.ConfigureResult()

    scenario.configure.side_effect = configure
    async with Client(server(scenario.service, scenario.workspace)) as client:
        await client.call_tool("cmake_configure", {})
        assert len(await scenario.service.compilation_contexts()) == 1
        if failure == "build_error_reply":

            async def failed_build(*_args, **_kwargs):
                """Write a CMake error reply while retaining the previous success index."""
                directory = (
                    scenario.workspace.storage_root
                    / "build/cmake-debug/.cmake/api/v1/reply"
                )
                (directory / "error-999999.json").write_text("{}")
                return cmake.BuildResult(return_code=1)

            scenario.build.side_effect = failed_build
            await client.call_tool("cmake_build", {})
        else:
            scenario.configure.side_effect = None
            scenario.configure.return_value = cmake.ConfigureResult(
                return_code=int(failure == "configure")
            )
            if failure == "invalid_reply":

                async def invalid_configure(*_args, **_kwargs):
                    """Write a new index whose referenced inputs are malformed."""
                    directory = write_generation(scenario)
                    (directory / ".cmake/api/v1/reply/cmakeFiles-2.json").write_text(
                        "{}"
                    )
                    return cmake.ConfigureResult()

                scenario.configure.side_effect = invalid_configure
            response = await client.call_tool("cmake_configure", {})
            assert response.structured_content["result"][0]["error"]
        assert await scenario.service.compilation_contexts() == []
        await scenario.service.publish_configurations(refresh=True)
        assert await scenario.service.compilation_contexts() == []
        assert not scenario.service.operation_lock.locked()


@pytest.mark.anyio
async def test_startup_requires_baseline_and_successful_configure_restores_context(
    request,
):
    """Treat even complete preexisting File API/database artifacts as unconfirmed at startup."""
    scenario = request.getfixturevalue("setup")
    scenario.service.definitions = (ProfileDefinition(name="debug"),)
    write_generation(scenario)
    assert await scenario.service.compilation_contexts() == []
    assert not scenario.service.inputs

    async def configure(*_args, **_kwargs):
        """Create a new generation that can confirm the current input contents."""
        write_generation(scenario)
        return cmake.ConfigureResult()

    scenario.configure.side_effect = configure
    async with Client(server(scenario.service, scenario.workspace)) as client:
        await client.call_tool("cmake_configure", {})
        assert len(await scenario.service.compilation_contexts()) == 1
        await client.call_tool(
            "workspace_write_file",
            {"path": "CMakeLists.txt", "text": "# changed"},
        )
        assert await scenario.service.compilation_contexts() == []
        await client.call_tool("cmake_configure", {})
        assert len(await scenario.service.compilation_contexts()) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("partial", [False, True])
async def test_workspace_error_checks_partial_changes_and_releases_lock(
    request, partial
):
    """Check partial writes and release locks after failed Workspace operations."""
    scenario = request.getfixturevalue("setup")
    directory = WorkspacePath("storage/build/cmake-debug")
    write_generation(scenario)
    scenario.service.inputs[directory] = read_generation(
        scenario.service.build_path(directory),
        None,
        scenario.workspace.root,
        scenario.workspace.storage_root,
    )
    call = await scenario.service.begin_workspace(
        (WorkspacePath("project/CMakeLists.txt"),)
    )
    if partial:
        (scenario.workspace.root / "CMakeLists.txt").write_text("# partial mutation")
    provider = CMakeProvider(scenario.service)
    await provider.error("failed-call", call)
    assert scenario.service.inputs[directory].current is not partial
    assert not scenario.service.operation_lock.locked()
    async with asyncio.timeout(2):
        call = await scenario.service.begin_workspace(
            (WorkspacePath("project/CMakeLists.txt"),)
        )
        await provider.error("next-call", call)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["configure", "build"])
async def test_cancelled_cmake_commands_withdraw_context_and_release_lock(
    request, operation
):
    """Cancellation must not strand command locks or retain a potentially stale context."""
    scenario = request.getfixturevalue("setup")
    scenario.service.definitions = (ProfileDefinition(name="debug"),)
    started = asyncio.Event()

    async def configure(*_args, **_kwargs):
        """Write the initial confirmed configuration."""
        write_generation(scenario)
        return cmake.ConfigureResult()

    async def interrupted(*_args, **_kwargs):
        """Expose a deterministic cancellation point during a CMake command."""
        started.set()
        await asyncio.Event().wait()

    scenario.configure.side_effect = configure
    async with Client(server(scenario.service)) as client:
        await client.call_tool("cmake_configure", {})
        getattr(scenario, operation).side_effect = interrupted
        task = asyncio.create_task(client.call_tool(f"cmake_{operation}", {}))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not scenario.service.operation_lock.locked()
    assert await scenario.service.compilation_contexts() == []


@pytest.mark.anyio
async def test_failed_profile_keeps_other_profile_confirmed(request):
    """A failed configure execution must leave another build tree and its snapshot active."""
    scenario = request.getfixturevalue("setup")
    scenario.service.definitions = tuple(
        (
            ProfileDefinition(name=name) for name in ("debug", "release")
        ),
    )

    async def configure(_source, build_directory, **_kwargs):
        """Generate the selected tree independently."""
        write_generation(scenario, build_directory.name.removeprefix("cmake-"))
        return cmake.ConfigureResult()

    scenario.configure.side_effect = configure
    async with Client(server(scenario.service)) as client:
        await client.call_tool("cmake_configure", {})
        scenario.configure.side_effect = None
        scenario.configure.return_value = cmake.ConfigureResult(return_code=1)
        await client.call_tool("cmake_configure", {"profiles": ["debug"]})
        assert [item.id for item in await scenario.service.compilation_contexts()] == [
            "release"
        ]


@pytest.mark.anyio
async def test_workspace_cancelled_before_stage_releases_cmake_context(request):
    """Release a completed CMake capture when another before hook is cancelled."""
    scenario = request.getfixturevalue("setup")
    started = asyncio.Event()

    class WaitingProvider(ResultProvider[object]):
        """Pause the before stage after CMake has had a chance to acquire its lock."""

        async def before_workspace_write_file(self, call_id, path, text):
            """Expose a cancellation point without holding a provider-owned lock."""
            started.set()
            await asyncio.Event().wait()

    scenario.workspace.register_provider(
        "cmake",
        CMakeProvider(scenario.service),
        tools=CMakeProvider.TOOLS,
    )
    scenario.workspace.register_provider("waiting", WaitingProvider())
    task = asyncio.create_task(
        scenario.workspace.before_providers(
            "workspace_write_file",
            {"path": WorkspacePath("project/CMakeLists.txt"), "text": "# edit"},
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not scenario.service.operation_lock.locked()
    assert not scenario.workspace.result_resources

@pytest.mark.anyio
@pytest.mark.parametrize("stage", ["before", "after"])
async def test_unreadable_input_releases_lock_and_preserves_independent_tree(
    request,
    monkeypatch,
    stage,
):
    """Withdraw only affected inputs when hash capture fails before or after a mutation."""
    scenario = request.getfixturevalue("setup")
    for name, dependency in (("debug", "CMakeLists.txt"), ("release", "README.md")):
        write_generation(scenario, name, inputs=[dependency])
        directory = WorkspacePath(f"storage/build/cmake-{name}")
        scenario.service.inputs[directory] = read_generation(
            scenario.service.build_path(directory),
            None,
            scenario.workspace.root,
            scenario.workspace.storage_root,
        )
    paths = (WorkspacePath("project/CMakeLists.txt"),)
    context = await scenario.service.begin_workspace(paths) if stage == "after" else None

    def unreadable(*_args, **_kwargs):
        """Simulate an input becoming unreadable without depending on platform ACL semantics."""
        raise OSError("input unavailable")

    monkeypatch.setattr("forgemcp.cmake.service.input_checksums", unreadable)
    with pytest.raises(OSError):
        if stage == "before":
            await scenario.service.begin_workspace(paths)
        else:
            await scenario.service.finish_workspace(context)
    assert not scenario.service.operation_lock.locked()
    assert not scenario.service.inputs[WorkspacePath("storage/build/cmake-debug")].current
    assert scenario.service.inputs[WorkspacePath("storage/build/cmake-release")].current
