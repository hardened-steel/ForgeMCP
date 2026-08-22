"""Workspace MCP contribution, CAS, and post-commit event regressions."""

from __future__ import annotations

import asyncio

import forgemcp.workspace.events as workspace_events

from forgemcp.core.application import ForgeApplication
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.workspace import WorkspaceMutationBatch, WorkspaceMutationBus, WorkspaceService


def _patch(path: str, old: str, new: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


def test_workspace_plugin_contributes_strict_schemas_and_cas_operations(tmp_path):
    async def exercise() -> None:
        (tmp_path / "note.txt").write_bytes(b"before\n")
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        await application.start()
        try:
            tools = {item.name: item for item in application.services.get("plugins").tools.contributions()}
            assert set(name for name in tools if name.startswith("workspace__")) == {
                "workspace__list_files", "workspace__read_text", "workspace__get_snapshot",
                "workspace__apply_unified_patch", "workspace__apply_text_edits",
            }
            assert tools["workspace__read_text"].input_model is not None
            assert tools["workspace__read_text"].input_model.model_config["extra"] == "forbid"

            listed = await tools["workspace__list_files"].handler({"recursive": True})
            assert listed["files"][0]["path"] == "note.txt"
            read = await tools["workspace__read_text"].handler({"path": "note.txt"})
            assert read["text"] == "before\n"
            assert str(tmp_path) not in str(read["snapshot"])
            digest = read["snapshot"]["sha256"]

            modified = await tools["workspace__apply_unified_patch"].handler(
                {"patch": _patch("note.txt", "before", "after"), "expected_snapshots": {"note.txt": digest}}
            )
            assert modified["applied"] is True
            created = await tools["workspace__apply_unified_patch"].handler(
                {
                    "patch": "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+created\n",
                    "expected_snapshots": {"new.txt": None},
                }
            )
            assert created["applied"] is True
            mixed_delete = await tools["workspace__apply_unified_patch"].handler(
                {
                    "patch": (
                        "--- /dev/null\n+++ b/another.txt\n@@ -0,0 +1 @@\n+created\n"
                        "--- a/note.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-after\n"
                    ),
                    "expected_snapshots": {
                        "another.txt": None,
                        "note.txt": (await tools["workspace__get_snapshot"].handler({"path": "note.txt"}))["snapshot"]["sha256"],
                    },
                }
            )
            assert mixed_delete["error"]["code"] == "workspace_request_error"
            assert (tmp_path / "note.txt").read_text(encoding="utf-8") == "after\n"
            assert not (tmp_path / "another.txt").exists()
            current = await tools["workspace__get_snapshot"].handler({"path": "note.txt"})
            edited = await tools["workspace__apply_text_edits"].handler(
                {
                    "edits_by_path": {"note.txt": [{"range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}}, "new_text": "final"}]},
                    "expected_snapshots": {"note.txt": current["snapshot"]["sha256"]},
                }
            )
            assert edited["applied"] is True
            stale = await tools["workspace__apply_text_edits"].handler(
                {
                    "edits_by_path": {"note.txt": [{"range": {"start": {"line": 0, "column": 0}, "end": {"line": 0, "column": 5}}, "new_text": "again"}]},
                    "expected_snapshots": {"note.txt": digest},
                }
            )
            assert stale == {"applied": False, "changes": []}
            invalid = await tools["workspace__read_text"].handler({"path": "note.txt", "unexpected": True})
            assert invalid["error"]["code"] == "workspace_request_error"
            assert "before" not in str(invalid)
        finally:
            await application.aclose()

    asyncio.run(exercise())


def test_workspace_mutation_bus_is_one_batch_post_commit_and_contains_no_content(tmp_path):
    async def exercise() -> None:
        (tmp_path / "one.txt").write_bytes(b"one\n")
        (tmp_path / "two.txt").write_bytes(b"two\n")
        logger = create_logger("CRITICAL")
        bus = WorkspaceMutationBus(logger)
        service = WorkspaceService(ForgeConfig(workspace_root=tmp_path), logger, mutations=bus)
        received: list[WorkspaceMutationBatch] = []
        bus.subscribe("test", received.append)
        await bus.start()
        result = service.apply_unified_patch(
            _patch("one.txt", "one", "ONE") + _patch("two.txt", "two", "TWO"),
            {"one.txt": service.get_snapshot("one.txt"), "two.txt": service.get_snapshot("two.txt")},
        )
        assert result.applied is True
        await asyncio.sleep(0)
        assert len(received) == 1
        batch = received[0]
        assert batch.generation == 1 and len(batch.changes) == 2
        assert [change.path for change in batch.changes] == ["one.txt", "two.txt"]
        assert "ONE" not in repr(batch) and "TWO" not in repr(batch)
        failed = service.apply_unified_patch(_patch("one.txt", "one", "again"), {"one.txt": "0" * 64})
        assert failed.applied is False
        await asyncio.sleep(0)
        assert len(received) == 1
        await bus.aclose()

    asyncio.run(exercise())


def test_workspace_mutation_subscriber_failure_degrades_without_rolling_back(tmp_path):
    async def exercise() -> None:
        (tmp_path / "note.txt").write_bytes(b"before\n")
        logger = create_logger("CRITICAL")
        bus = WorkspaceMutationBus(logger)
        service = WorkspaceService(ForgeConfig(workspace_root=tmp_path), logger, mutations=bus)

        def fail(_: WorkspaceMutationBatch) -> None:
            raise RuntimeError("integration failure must be contained")

        bus.subscribe("failing", fail)
        await bus.start()
        result = service.apply_unified_patch(
            _patch("note.txt", "before", "after"), {"note.txt": service.get_snapshot("note.txt")}
        )
        assert result.applied is True
        await asyncio.sleep(0)
        assert bus.degraded is True
        assert (tmp_path / "note.txt").read_bytes() == b"after\n"
        await bus.aclose()

    asyncio.run(exercise())


def test_workspace_mutation_batch_is_path_sorted_and_noop_does_not_publish(tmp_path):
    async def exercise() -> None:
        (tmp_path / "one.txt").write_text("one\n", encoding="utf-8")
        (tmp_path / "two.txt").write_text("two\n", encoding="utf-8")
        logger = create_logger("CRITICAL")
        bus = WorkspaceMutationBus(logger)
        service = WorkspaceService(ForgeConfig(workspace_root=tmp_path), logger, mutations=bus)
        received: list[WorkspaceMutationBatch] = []
        bus.subscribe("test", received.append)
        await bus.start()
        result = service.apply_unified_patch(
            _patch("two.txt", "two", "TWO") + _patch("one.txt", "one", "ONE"),
            {"one.txt": service.get_snapshot("one.txt"), "two.txt": service.get_snapshot("two.txt")},
        )
        assert result.applied is True
        await asyncio.sleep(0)
        assert [change.path for change in received[0].changes] == ["one.txt", "two.txt"]
        no_op = service.apply_unified_patch(
            "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n ONE\n",
            {"one.txt": service.get_snapshot("one.txt")},
        )
        assert no_op.applied is True and no_op.changes == ()
        await asyncio.sleep(0)
        assert len(received) == 1
        await bus.aclose()

    asyncio.run(exercise())


def test_workspace_mutation_bus_bounds_cancellation_suppressing_subscriber_shutdown(tmp_path, monkeypatch):
    async def exercise() -> None:
        (tmp_path / "note.txt").write_text("before\n", encoding="utf-8")
        logger = create_logger("CRITICAL")
        bus = WorkspaceMutationBus(logger)
        service = WorkspaceService(ForgeConfig(workspace_root=tmp_path), logger, mutations=bus)
        released = asyncio.Event()

        async def suppress_cancel(_: WorkspaceMutationBatch) -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await released.wait()

        monkeypatch.setattr(workspace_events, "SUBSCRIBER_HANDLER_TIMEOUT_SECONDS", 0.01)
        monkeypatch.setattr(workspace_events, "SUBSCRIBER_CLOSE_TIMEOUT_SECONDS", 0.01)
        bus.subscribe("stuck", suppress_cancel)
        await bus.start()
        assert service.apply_unified_patch(
            _patch("note.txt", "before", "after"), {"note.txt": service.get_snapshot("note.txt")}
        ).applied
        await asyncio.sleep(0.03)
        assert bus.degraded is True
        await asyncio.wait_for(bus.aclose(), timeout=0.2)
        released.set()
        await asyncio.sleep(0)

    asyncio.run(exercise())
