"""Project Intelligence Phase 1 registry, aggregation, and composition tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from forgemcp.core import ForgeApplication, ForgeConfig
from forgemcp.project import (
    ComponentState,
    ComponentStatus,
    DuplicateProjectStatusProviderError,
    ProjectActivity,
    ProjectHealth,
    ProjectStatusRegistry,
    ProjectStatusRegistryClosedError,
    ProjectStatusService,
    StatusFact,
)
from forgemcp.project.models import utc_now
from forgemcp.plugins import ForgePlugin, PluginContext, PluginMetadata
from forgemcp.server import create_server
from forgemcp.models import ProcessOutput, ProcessResult
from forgemcp.clangd.models import ClangdSessionState
from forgemcp.debugger.models import DebuggerState
from forgemcp.quality.errors import QualityToolExecutionError


class _Provider:
    def __init__(
        self,
        provider_id: str,
        *,
        state: ComponentState = ComponentState.IDLE,
        delay: float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.id = provider_id
        self.state = state
        self.delay = delay
        self.error = error
        self.cancelled = False

    async def snapshot_status(self) -> ComponentStatus:
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            return ComponentStatus(
                id=self.id,
                display_name=self.id,
                state=self.state,
                summary="safe cached status",
                observed_at=utc_now(),
            )
        finally:
            self.cancelled = self.cancelled or asyncio.current_task().cancelling() > 0


def test_models_are_strict_immutable_and_bounded() -> None:
    fact = StatusFact(name="count", value=1)
    with pytest.raises(ValidationError):
        StatusFact.model_validate({"name": "count", "value": 1, "raw": "secret"})
    with pytest.raises(ValidationError):
        StatusFact(name="value", value="x" * 257)
    with pytest.raises(ValidationError):
        ComponentStatus(
            id="component",
            display_name="component",
            state=ComponentState.IDLE,
            summary="safe",
            warnings=tuple("warning" for _ in range(33)),
            observed_at=utc_now(),
        )
    with pytest.raises(ValidationError):
        fact.value = 2  # type: ignore[misc]


def test_registry_registration_duplicate_unregister_and_ordering() -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        registry.register(_Provider("zeta"))
        registry.register(_Provider("alpha"))
        with pytest.raises(DuplicateProjectStatusProviderError):
            registry.register(_Provider("alpha"))
        assert registry.provider_ids() == ("alpha", "zeta")
        snapshot = await registry.snapshot_all()
        assert tuple(item.id for item in snapshot.components) == ("alpha", "zeta")
        registry.unregister("missing")
        registry.unregister("alpha")
        registry.unregister("alpha")
        assert registry.provider_ids() == ("zeta",)

    asyncio.run(exercise())


def test_registry_provider_failure_timeout_and_global_timeout_are_partial_safe() -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        registry.register(_Provider("healthy"))
        registry.register(_Provider("secret_failure", error=RuntimeError("password=hunter2")))
        slow = _Provider("slow", delay=10)
        registry.register(slow)
        snapshot = await registry.snapshot_all(
            provider_timeout_seconds=5.0, total_timeout_seconds=0.02
        )
        assert tuple(item.id for item in snapshot.components) == ("healthy",)
        assert snapshot.failed_components == ("secret_failure",)
        assert snapshot.timed_out_components == ("slow",)
        assert slow.cancelled is True
        service = ProjectStatusService(registry, Path("C:/workspace"), provider_timeout_seconds=0.01)
        status = await service.status()
        serialized = status.model_dump_json()
        assert status.partial is True
        assert "hunter2" not in serialized
        assert "RuntimeError" not in serialized

    asyncio.run(exercise())


def test_registry_rejects_arbitrary_and_oversized_provider_results() -> None:
    class BadProvider:
        id = "bad"

        async def snapshot_status(self):
            return {"id": "bad", "raw": "source text"}

    class OversizedProvider:
        id = "oversized"

        async def snapshot_status(self):
            return ComponentStatus.model_construct(
                id="oversized",
                display_name="oversized",
                state=ComponentState.IDLE,
                capabilities=(),
                summary="safe",
                facts=(),
                warnings=tuple("warning" for _ in range(33)),
                stale=False,
                observed_at=utc_now(),
            )

    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        registry.register(BadProvider())
        registry.register(OversizedProvider())
        result = await registry.snapshot_all()
        assert result.components == ()
        assert result.failed_components == ("bad", "oversized")

    asyncio.run(exercise())


def test_caller_cancellation_and_registry_shutdown_join_provider_tasks() -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        provider = _Provider("waiting", delay=10)
        registry.register(provider)
        task = asyncio.create_task(registry.snapshot_all(total_timeout_seconds=20))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert provider.cancelled is True
        assert not registry._active_tasks
        await registry.aclose()
        with pytest.raises(ProjectStatusRegistryClosedError):
            await registry.snapshot_all()

    asyncio.run(exercise())


def test_unregister_during_snapshot_affects_only_future_calls() -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        provider = _Provider("transient", delay=0.01)
        registry.register(provider)
        current = asyncio.create_task(registry.snapshot_all())
        await asyncio.sleep(0)
        registry.unregister("transient")
        assert tuple(item.id for item in (await current).components) == ("transient",)
        assert (await registry.snapshot_all()).components == ()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("components", "partial", "health", "activity"),
    [
        (("core", ComponentState.FAILED), False, ProjectHealth.FAILED, ProjectActivity.IDLE),
        (("clangd", ComponentState.FAILED), False, ProjectHealth.DEGRADED, ProjectActivity.IDLE),
        (("debugger", ComponentState.PAUSED), False, ProjectHealth.HEALTHY, ProjectActivity.PAUSED),
        (("cmake", ComponentState.ACTIVE), False, ProjectHealth.HEALTHY, ProjectActivity.BUSY),
    ],
)
def test_health_and_activity_rules(components, partial, health, activity) -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        registry.register(_Provider(components[0], state=components[1]))
        if partial:
            registry.register(_Provider("broken", error=RuntimeError("secret")))
        status = await ProjectStatusService(registry, Path("C:/workspace")).status()
        assert status.health is health
        assert status.activity is activity

    asyncio.run(exercise())


def test_failed_build_warning_does_not_make_cmake_service_failed() -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        cmake = _Provider("cmake", state=ComponentState.IDLE)
        registry.register(cmake)
        status = await ProjectStatusService(registry, Path("C:/workspace")).status()
        assert status.health is ProjectHealth.HEALTHY

    asyncio.run(exercise())


def test_application_registers_all_builtin_providers_and_status_is_side_effect_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        await application.start()
        registry = application.services.get("project_status_registry")
        assert isinstance(registry, ProjectStatusRegistry)
        assert registry.provider_ids() == (
            "clangd", "cmake", "core", "debugger", "plugin_manager",
            "process_runtime", "quality", "workspace",
        )
        runtime = application.services.get("process_runtime")
        workspace = application.services.get("workspace")

        async def forbidden_process(*args, **kwargs):
            raise AssertionError("project status must not run processes")

        monkeypatch.setattr(runtime, "run", forbidden_process)
        monkeypatch.setattr(workspace, "read_text", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("source read")))
        monkeypatch.setattr(workspace, "list_files", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("file listing")))
        service = application.services.get("project_status_service")
        first, second = await asyncio.gather(service.status(), service.status())
        assert len(first.components) == len(second.components) == 8
        assert first.partial is False
        await application.aclose()
        with pytest.raises(ProjectStatusRegistryClosedError):
            await service.status()

    asyncio.run(exercise())


def test_multiple_applications_do_not_share_project_status_state(tmp_path: Path) -> None:
    async def exercise() -> None:
        first_root = tmp_path / "one"
        second_root = tmp_path / "two"
        first_root.mkdir()
        second_root.mkdir()
        first = ForgeApplication.create(ForgeConfig(workspace_root=first_root))
        second = ForgeApplication.create(ForgeConfig(workspace_root=second_root))
        await first.start()
        await second.start()
        first_registry = first.services.get("project_status_registry")
        second_registry = second.services.get("project_status_registry")
        first_registry.unregister("quality")
        assert "quality" not in first_registry.provider_ids()
        assert "quality" in second_registry.provider_ids()
        assert (await first.services.get("project_status_service").status()).workspace_root != (
            await second.services.get("project_status_service").status()
        ).workspace_root
        await first.aclose()
        await second.aclose()

    asyncio.run(exercise())


def test_mcp_project_status_returns_partial_for_one_external_provider_failure(
    tmp_path: Path,
) -> None:
    class FailingProviderPlugin(ForgePlugin):
        def __init__(self) -> None:
            super().__init__(
                PluginMetadata(
                    plugin_id="status_extension",
                    requires_services=("project_status_registry",),
                )
            )
            self.registry = None

        async def start(self, context: PluginContext) -> None:
            registry = context.services.get("project_status_registry")
            assert isinstance(registry, ProjectStatusRegistry)
            self.registry = registry
            registry.register(_Provider("external_component", error=RuntimeError("api_key=secret")))

        async def stop(self) -> None:
            if self.registry is not None:
                self.registry.unregister("external_component")

    async def exercise() -> None:
        application = ForgeApplication.create(
            ForgeConfig(workspace_root=tmp_path), builtin_plugins=(FailingProviderPlugin(),)
        )
        server = create_server(lambda: application)
        async with server._mcp_server.lifespan(server._mcp_server):  # type: ignore[attr-defined]
            content = await server.call_tool("project__status", {})
            payload = json.loads(content[0].text)
            assert payload["partial"] is True
            assert payload["health"] == "degraded"
            assert "component_status_failed:external_component" in payload["warnings"]
            assert "api_key" not in content[0].text

    asyncio.run(exercise())


def test_project_status_during_cmake_build_is_busy_and_failed_result_stays_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        (tmp_path / "build").mkdir()
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        await application.start()
        plugins = application.services.get("plugins")
        cmake_plugin = plugins._records["cmake"].plugin
        runtime = application.services.get("process_runtime")
        entered = asyncio.Event()
        release = asyncio.Event()

        async def failed_build(*args, **kwargs):
            entered.set()
            await release.wait()
            now = datetime.now(UTC)
            return ProcessResult(
                exit_code=2,
                started_at=now,
                finished_at=now,
                stdout=ProcessOutput(text="compiler output intentionally not exposed"),
                stderr=ProcessOutput(text="diagnostic text intentionally not exposed"),
            )

        monkeypatch.setattr(runtime, "run", failed_build)
        build = asyncio.create_task(cmake_plugin.service.build(binary_dir="build"))
        await entered.wait()
        service = application.services.get("project_status_service")
        active = await service.status()
        cmake_active = next(item for item in active.components if item.id == "cmake")
        assert cmake_active.state is ComponentState.ACTIVE
        assert active.activity is ProjectActivity.BUSY
        assert "compiler output" not in active.model_dump_json()
        release.set()
        result = await build
        assert result.process.exit_code == 2
        completed = await service.status()
        cmake_completed = next(item for item in completed.components if item.id == "cmake")
        assert cmake_completed.state is ComponentState.IDLE
        assert "last_build_failure" in cmake_completed.warnings
        assert completed.health is ProjectHealth.HEALTHY
        serialized = completed.model_dump_json()
        assert "compiler output" not in serialized
        assert "diagnostic text" not in serialized
        await application.aclose()

    asyncio.run(exercise())


def test_clangd_and_debugger_cached_session_states_drive_health_and_activity(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        await application.start()
        plugins = application.services.get("plugins")
        service = application.services.get("project_status_service")
        clangd = plugins._records["clangd"].plugin.service
        debugger = plugins._records["debugger"].plugin.service

        clangd._state = ClangdSessionState.STARTING
        assert (await service.status()).activity is ProjectActivity.BUSY
        clangd._state = ClangdSessionState.FAILED
        assert (await service.status()).health is ProjectHealth.DEGRADED
        clangd._state = ClangdSessionState.STOPPED

        async with debugger._state_lock:
            debugger._state = DebuggerState.RUNNING
        assert (await service.status()).activity is ProjectActivity.BUSY
        async with debugger._state_lock:
            debugger._state = DebuggerState.PAUSED
        paused = await service.status()
        assert paused.activity is ProjectActivity.PAUSED
        assert next(item for item in paused.components if item.id == "debugger").state is ComponentState.PAUSED
        await application.aclose()

    asyncio.run(exercise())


def test_project_status_during_quality_operation_is_busy_without_waiting_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        await application.start()
        plugins = application.services.get("plugins")
        quality = plugins._records["quality"].plugin
        contribution = next(
            item for item in plugins.tools.contributions() if item.name == "clang_format__check"
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_check(paths):
            entered.set()
            await release.wait()
            raise QualityToolExecutionError("Formatting failed safely.")

        monkeypatch.setattr(quality.clang_format, "check", blocked_check)
        operation = asyncio.create_task(contribution.handler({"paths": ["main.cpp"]}))
        await entered.wait()
        status = await application.services.get("project_status_service").status()
        assert status.activity is ProjectActivity.BUSY
        assert next(item for item in status.components if item.id == "quality").state is ComponentState.ACTIVE
        release.set()
        response = await operation
        assert response["error"]["code"] == "quality_tool_execution_error"
        await application.aclose()

    asyncio.run(exercise())
