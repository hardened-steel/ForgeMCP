"""Batch mutations coordinate retained analysis sessions through CMake-owned contexts."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forgemcp.clangd.service import ClangdService, FileChange, RunningSession
from forgemcp.cmake.file_api import ConfigurationInputs, checksum
from forgemcp.cmake.models import CompilationContext
from forgemcp.cmake.service import CMakeService
from forgemcp.process.service import ProcessService
from forgemcp.toolchain.service import ToolchainService
from forgemcp.toolchain.spec import ToolKind, Toolset, ToolSpec
from forgemcp.workspace.path import WorkspacePath
from forgemcp.workspace.service import WorkspaceService


@pytest.fixture
def anyio_backend():
    """Use the asyncio backend required by process and retained analysis services."""
    return "asyncio"


@pytest.fixture(name="source_analysis")
async def source_analysis_fixture(
    cpp_acceptance_project,
    monkeypatch,
) -> AsyncGenerator[SimpleNamespace]:
    """Retain two real CMake contexts with lightweight observable language-server sessions."""
    workspace = WorkspaceService(cpp_acceptance_project)
    processes = ProcessService(workspace.root, allowed_roots=(workspace.storage_root,))
    toolchains = ToolchainService(processes)
    tool = ToolSpec("clangd", ToolKind.LANGUAGE_SERVER, workspace.root / "clangd", {})
    toolchains.toolsets = (Toolset("llvm", "LLVM", (tool,), None, True),)
    cmake = CMakeService(
        toolchains,
        processes,
        protected_paths=workspace.protected_paths,
        storage_root=workspace.storage_root,
        project_root=workspace.root,
    )
    for name in ("debug", "release"):
        native = workspace.storage_root / f"build/{name}"
        native.mkdir(parents=True)
        (native / "compile_commands.json").write_text("[]", encoding="utf-8")
        directory = WorkspacePath(f"storage/build/{name}")
        cmake.inputs[directory] = ConfigurationInputs(
            {WorkspacePath("project/CMakeLists.txt"): checksum(workspace.root / "CMakeLists.txt")},
            current=True,
        )
        cmake.configurations[name] = CompilationContext(
            id=name,
            toolset_id="llvm",
            build_directory=directory,
            compilation_database=WorkspacePath(f"{directory}/compile_commands.json"),
        )
    service = ClangdService(workspace, toolchains, cmake)
    peers = []

    async def retained_session(context, _on_progress, _timeout):
        """Replace only subprocess startup with a retained, observable session lifetime."""
        if context.id not in service.sessions:
            session = SimpleNamespace(
                configuration=context,
                failure=None,
                notify=AsyncMock(),
                shutdown=AsyncMock(),
            )
            peers.append(session)
            service.sessions[context.id] = RunningSession(
                session,
                AsyncExitStack(),
                service.fingerprint(context),
            )
        return service.sessions[context.id].session

    monkeypatch.setattr(service, "session", retained_session)
    await service.update_configurations(await cmake.compilation_contexts())
    try:
        yield SimpleNamespace(workspace=workspace, cmake=cmake, service=service, peers=peers)
    finally:
        await service.close()
        await processes.close()


@pytest.mark.anyio
async def test_source_change_reconciles_cmake_without_restarting_unchanged_sessions(
    source_analysis,
):
    """Source notifications preserve healthy sessions when CMake contexts remain current."""
    scenario = source_analysis
    original = {name: running.session for name, running in scenario.service.sessions.items()}
    async with scenario.service.source_change():
        assert scenario.service.lock.locked()
        context = await scenario.cmake.begin_source_change()
        source = scenario.workspace.root / "src/math.cpp"
        source.write_text(source.read_text() + "\n// Git source edit\n")
        await scenario.cmake.finish_source_change(context)
        await scenario.service.refresh_source_changes(
            [FileChange(WorkspacePath("project/src/math.cpp"), 2)],
        )
    assert not scenario.service.lock.locked()
    for name, running in scenario.service.sessions.items():
        assert running.session is original[name]
    for peer in scenario.peers:
        args = peer.notify.await_args
        assert args.args[0] == "workspace/didChangeWatchedFiles"
        assert args.args[1]["changes"][0]["type"] == 2


@pytest.mark.anyio
async def test_source_change_closes_contexts_with_changed_cmake_inputs(source_analysis):
    """Input invalidation reaches clangd through CMake's context selection, not direct edits."""
    scenario = source_analysis
    async with scenario.service.source_change():
        context = await scenario.cmake.begin_source_change()
        (scenario.workspace.root / "CMakeLists.txt").write_text("# changed by checkout\n")
        await scenario.cmake.finish_source_change(context)
        await scenario.service.refresh_source_changes(
            [FileChange(WorkspacePath("project/CMakeLists.txt"), 2)],
        )
        assert not scenario.service.sessions
        assert not scenario.service.contexts
    assert all(peer.shutdown.await_count == 1 for peer in scenario.peers)
    assert not scenario.cmake.operation_lock.locked()


@pytest.mark.anyio
async def test_unknown_source_changes_reset_indexes_and_keep_confirmed_contexts(source_analysis):
    """Unknown changed paths replace indexes without discarding CMake's valid configurations."""
    scenario = source_analysis
    original = {name: running.session for name, running in scenario.service.sessions.items()}
    old_peers = list(scenario.peers)
    async with scenario.service.source_change():
        context = await scenario.cmake.begin_source_change()
        await scenario.cmake.finish_source_change(context)
        await scenario.service.refresh_source_changes(None)
    assert set(scenario.service.sessions) == set(original)
    assert all(
        running.session is not original[name]
        for name, running in scenario.service.sessions.items()
    )
    assert all(peer.shutdown.await_count == 1 for peer in old_peers)
    assert not scenario.service.lock.locked()


@pytest.mark.anyio
async def test_source_change_cancellation_closes_sessions_and_releases_lock(source_analysis):
    """Cancelled batch callers cannot leave an active analysis lock or stale retained sessions."""
    scenario = source_analysis
    started = asyncio.Event()

    async def mutation():
        """Hold the source interval until the test cancels its owning task."""
        async with scenario.service.source_change():
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(mutation())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not scenario.service.lock.locked()
    assert not scenario.service.sessions
    assert all(peer.shutdown.await_count == 1 for peer in scenario.peers)
