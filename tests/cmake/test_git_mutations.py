"""Batch source changes reuse CMake's controlled input-freshness policy."""

import asyncio

import pytest

from forgemcp.cmake.file_api import ConfigurationInputs, checksum
from forgemcp.cmake.models import CompilationContext
from forgemcp.workspace.path import WorkspacePath

def confirmed_trees(scenario):
    """Create independent confirmed trees with different inputs and structural policies."""
    for name, dependency, structural in (
        ("debug", "CMakeLists.txt", True),
        ("release", "README.md", False),
    ):
        native = scenario.workspace.storage_root / f"build/cmake-{name}"
        native.mkdir(parents=True)
        (native / "compile_commands.json").write_text("[]", encoding="utf-8")
        directory = WorkspacePath(f"storage/build/cmake-{name}")
        reference = WorkspacePath(f"project/{dependency}")
        scenario.service.inputs[directory] = ConfigurationInputs(
            {reference: checksum(scenario.workspace.root / dependency)},
            structural=structural,
            current=True,
        )
        scenario.service.configurations[name] = CompilationContext(
            id=name,
            toolset_id="system",
            build_directory=directory,
            compilation_database=WorkspacePath(f"{directory}/compile_commands.json"),
        )


@pytest.mark.anyio
async def test_batch_change_withdraws_only_changed_input_and_publishes(request):
    """Detect changed configure inputs without requiring a list of individual Git paths."""
    scenario = request.getfixturevalue("setup")
    confirmed_trees(scenario)
    updates = scenario.service.configuration_updates()
    assert {item.id for item in await anext(updates)} == {"debug", "release"}
    try:
        context = await scenario.service.begin_source_change()
        (scenario.workspace.root / "CMakeLists.txt").write_text("# checked out revision\n")
        await scenario.service.finish_source_change(context)
        assert not scenario.service.operation_lock.locked()
        assert [item.id for item in await asyncio.wait_for(anext(updates), 5)] == ["release"]
        assert [item.id for item in await scenario.service.compilation_contexts()] == ["release"]
    finally:
        await updates.aclose()


@pytest.mark.anyio
async def test_batch_source_edits_and_identical_inputs_preserve_contexts(request):
    """A source-only edit and identical input rewrite do not withdraw build contexts."""
    scenario = request.getfixturevalue("setup")
    confirmed_trees(scenario)
    context = await scenario.service.begin_source_change()
    cmake_input = scenario.workspace.root / "CMakeLists.txt"
    cmake_input.write_bytes(cmake_input.read_bytes())
    source = scenario.workspace.root / "src/math.cpp"
    source.write_text(source.read_text() + "\n// changed by Git\n")
    await scenario.service.finish_source_change(context)
    contexts = await scenario.service.compilation_contexts()
    assert {item.id for item in contexts} == {"debug", "release"}
    assert not scenario.service.operation_lock.locked()


@pytest.mark.anyio
async def test_batch_structural_changes_only_withdraw_structural_dependencies(request):
    """Caller-supplied additions invalidate glob-dependent trees without scanning directories."""
    scenario = request.getfixturevalue("setup")
    confirmed_trees(scenario)
    context = await scenario.service.begin_source_change()
    (scenario.workspace.root / "src/added.cpp").write_text("int added;\n")
    await scenario.service.finish_source_change(context, structural=True)
    assert [item.id for item in await scenario.service.compilation_contexts()] == ["release"]


@pytest.mark.anyio
async def test_uncertain_batch_outcome_withdraws_every_context(request):
    """Unknown hook effects or an interrupted command cannot retain confirmed contexts."""
    scenario = request.getfixturevalue("setup")
    confirmed_trees(scenario)
    context = await scenario.service.begin_source_change()
    await scenario.service.finish_source_change(context, uncertain=True)
    assert await scenario.service.compilation_contexts() == []
    assert not scenario.service.operation_lock.locked()


@pytest.mark.anyio
async def test_batch_verification_failure_releases_lock(request, monkeypatch):
    """An unreadable batch input withdraws affected contexts and releases the CMake lock."""
    scenario = request.getfixturevalue("setup")
    confirmed_trees(scenario)
    context = await scenario.service.begin_source_change()

    def unreadable(*_args, **_kwargs):
        """Simulate an input becoming unreadable after a source mutation."""
        raise OSError("cannot read input")

    monkeypatch.setattr("forgemcp.cmake.service.input_checksums", unreadable)
    with pytest.raises(OSError, match="cannot read input"):
        await scenario.service.finish_source_change(context)
    assert not scenario.service.operation_lock.locked()
    assert await scenario.service.compilation_contexts() == []


@pytest.mark.anyio
async def test_cancelled_batch_wait_does_not_release_another_operation(request):
    """Cancellation while waiting leaves the active mutation's lock ownership intact."""
    scenario = request.getfixturevalue("setup")
    confirmed_trees(scenario)
    context = await scenario.service.begin_source_change()
    waiting = asyncio.create_task(scenario.service.begin_source_change())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert scenario.service.operation_lock.locked()
    await scenario.service.finish_source_change(context)
    assert not scenario.service.operation_lock.locked()
