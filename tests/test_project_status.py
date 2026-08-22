"""Project Intelligence Phase 1 registry, aggregation, and composition tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from datetime import UTC, datetime, timedelta
from time import monotonic
from types import SimpleNamespace

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
from forgemcp.project.models import MAX_STATUS_JSON_BYTES, utc_now
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
        capabilities: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.id = provider_id
        self.state = state
        self.delay = delay
        self.error = error
        self.capabilities = capabilities
        self.warnings = warnings
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
                capabilities=self.capabilities,
                summary="safe cached status",
                warnings=self.warnings,
                observed_at=utc_now(),
            )
        finally:
            self.cancelled = self.cancelled or asyncio.current_task().cancelling() > 0


class _ResultProvider:
    def __init__(self, provider_id: str, result: object) -> None:
        self.id = provider_id
        self._result = result

    async def snapshot_status(self):
        return self._result


def _register_health_providers(
    registry: ProjectStatusRegistry,
    *components: tuple[str, ComponentState],
) -> None:
    states = {
        "core": ComponentState.ACTIVE,
        "workspace": ComponentState.AVAILABLE,
        "process_runtime": ComponentState.AVAILABLE,
        "plugin_manager": ComponentState.AVAILABLE,
    }
    overrides = dict(components)
    for provider_id, state in states.items():
        registry.register(_Provider(provider_id, state=overrides.get(provider_id, state)))
    for provider_id, state in components:
        if provider_id in states:
            continue
        registry.register(_Provider(provider_id, state=state))


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


def test_registry_capacity_and_deadline_arguments_are_bounded() -> None:
    registry = ProjectStatusRegistry()
    for index in range(64):
        registry.register(_Provider(f"provider{index:02d}"))
    with pytest.raises(ValueError, match="capacity"):
        registry.register(_Provider("overflow"))

    async def exercise() -> None:
        for invalid in (0, -1, float("inf"), float("nan"), True, "1"):
            with pytest.raises((TypeError, ValueError)):
                await ProjectStatusRegistry().snapshot_all(  # type: ignore[arg-type]
                    provider_timeout_seconds=invalid
                )

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


@pytest.mark.parametrize(
    "provider_id",
    ("Core", "CORE", "core/child", "core id", "сore", "ıd", "a\N{COMBINING ACUTE ACCENT}"),
)
def test_registry_rejects_case_variants_and_unicode_confusable_ids(provider_id: str) -> None:
    registry = ProjectStatusRegistry()
    with pytest.raises(ValueError, match="provider id is invalid"):
        registry.register(_Provider(provider_id))


def test_registry_revalidates_model_construct_and_temporal_constraints() -> None:
    base = {
        "id": "invalid",
        "display_name": "invalid",
        "state": ComponentState.IDLE,
        "capabilities": (),
        "summary": "safe",
        "facts": (),
        "warnings": (),
        "stale": False,
        "observed_at": utc_now(),
    }
    invalid_results = [
        ComponentStatus.model_construct(**{**base, "state": "bogus"}),
        ComponentStatus.model_construct(**{**base, "summary": "x" * 513}),
        ComponentStatus.model_construct(
            **{**base, "capabilities": tuple(f"cap{index}" for index in range(129))}
        ),
        ComponentStatus.model_construct(**{**base, "capabilities": ("dup", "dup")}),
        ComponentStatus.model_construct(
            **{
                **base,
                "facts": tuple(
                    StatusFact.model_construct(name=f"fact{index}", value=index)
                    for index in range(33)
                ),
            }
        ),
        ComponentStatus.model_construct(
            **{
                **base,
                "facts": (
                    StatusFact.model_construct(name="dup", value=1),
                    StatusFact.model_construct(name="dup", value=2),
                ),
            }
        ),
        ComponentStatus.model_construct(
            **{**base, "facts": (StatusFact.model_construct(name="value", value=1.5),)}
        ),
        ComponentStatus.model_construct(
            **{**base, "facts": (StatusFact.model_construct(name="value", value=[]),)}
        ),
        ComponentStatus.model_construct(
            **{**base, "facts": (StatusFact.model_construct(name="value", value={}),)}
        ),
        ComponentStatus.model_construct(**{**base, "observed_at": datetime.now()}),
        ComponentStatus(**{**base, "observed_at": utc_now() + timedelta(minutes=1)}),
        ComponentStatus(**{**base, "observed_at": utc_now() - timedelta(days=2)}),
    ]

    class ExtendedComponent(ComponentStatus):
        secret: str

    invalid_results.append(ExtendedComponent(**base, secret="token=hunter2"))

    async def exercise() -> None:
        for index, result in enumerate(invalid_results):
            registry = ProjectStatusRegistry()
            provider_id = f"invalid{index}"
            payload = result.model_copy(update={"id": provider_id})
            registry.register(_ResultProvider(provider_id, payload))
            snapshot = await registry.snapshot_all()
            assert snapshot.components == ()
            assert snapshot.failed_components == (provider_id,)
        mismatch = ProjectStatusRegistry()
        mismatch.register(_ResultProvider("registered", ComponentStatus(**{**base, "id": "different"})))
        assert (await mismatch.snapshot_all()).failed_components == ("registered",)

    asyncio.run(exercise())


def test_registry_defines_stale_age_policy_and_fact_scalar_types() -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        observed = utc_now() - timedelta(minutes=10)
        registry.register(
            _ResultProvider(
                "aged",
                ComponentStatus(
                    id="aged",
                    display_name="aged",
                    state=ComponentState.IDLE,
                    summary="safe",
                    observed_at=observed,
                ),
            )
        )
        result = await registry.snapshot_all()
        assert result.components[0].stale is True
        assert result.components[0].observed_at == observed

    assert type(StatusFact(name="flag", value=True).value) is bool
    assert type(StatusFact(name="count", value=1).value) is int
    with pytest.raises(ValidationError):
        StatusFact(name="huge", value=1 << 80)
    asyncio.run(exercise())


def test_provider_deadlines_remain_bounded_when_cancellation_is_suppressed() -> None:
    class SuppressingProvider:
        id = "suppressing"

        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.cancelled = False

        async def snapshot_status(self) -> ComponentStatus:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                await self.release.wait()
            return ComponentStatus(
                id=self.id,
                display_name=self.id,
                state=ComponentState.IDLE,
                summary="safe",
                observed_at=utc_now(),
            )

    async def exercise() -> None:
        provider = SuppressingProvider()
        registry = ProjectStatusRegistry()
        registry.register(provider)
        started = monotonic()
        result = await registry.snapshot_all(
            provider_timeout_seconds=0.01, total_timeout_seconds=0.02
        )
        elapsed = monotonic() - started
        assert elapsed < 0.20
        assert result.timed_out_components == ("suppressing",)
        assert provider.cancelled is True
        assert registry._active_tasks
        provider.release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not registry._active_tasks
        await registry.aclose()

    asyncio.run(exercise())


def test_multiple_slow_and_self_cancelled_providers_are_classified_without_serial_delay() -> None:
    class SelfCancelled(_Provider):
        async def snapshot_status(self) -> ComponentStatus:
            raise asyncio.CancelledError

    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        for index in range(8):
            registry.register(_Provider(f"slow{index}", delay=10))
        registry.register(SelfCancelled("cancelled"))
        started = monotonic()
        snapshot = await registry.snapshot_all(
            provider_timeout_seconds=0.02,
            total_timeout_seconds=0.5,
        )
        assert monotonic() - started < 0.20
        assert snapshot.failed_components == ("cancelled",)
        assert snapshot.timed_out_components == tuple(f"slow{index}" for index in range(8))
        assert not registry._active_tasks

    asyncio.run(exercise())


def test_overlapping_calls_are_single_flight_and_client_cancellation_isolated() -> None:
    class CountingProvider(_Provider):
        def __init__(self) -> None:
            super().__init__("counting")
            self.calls = 0
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def snapshot_status(self) -> ComponentStatus:
            self.calls += 1
            self.entered.set()
            await self.release.wait()
            return await super().snapshot_status()

    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        provider = CountingProvider()
        registry.register(provider)
        calls = [asyncio.create_task(registry.snapshot_all()) for _ in range(32)]
        await provider.entered.wait()
        calls[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await calls[0]
        assert provider.cancelled is False
        assert provider.calls == 1
        provider.release.set()
        results = await asyncio.gather(*calls[1:])
        assert all(result.components[0].id == "counting" for result in results)
        provider.release = asyncio.Event()
        provider.entered = asyncio.Event()
        next_call = asyncio.create_task(registry.snapshot_all())
        await provider.entered.wait()
        assert provider.calls == 2
        provider.release.set()
        await next_call
        await registry.aclose()

    asyncio.run(exercise())


def test_failed_single_flight_result_is_not_cached() -> None:
    class RecoveringProvider(_Provider):
        def __init__(self) -> None:
            super().__init__("recovering")
            self.calls = 0

        async def snapshot_status(self) -> ComponentStatus:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("first failure secret")
            return await super().snapshot_status()

    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        provider = RecoveringProvider()
        registry.register(provider)
        first = await registry.snapshot_all()
        second = await registry.snapshot_all()
        assert first.failed_components == ("recovering",)
        assert tuple(item.id for item in second.components) == ("recovering",)
        assert provider.calls == 2

    asyncio.run(exercise())


def test_global_response_size_is_deterministically_bounded() -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        _register_health_providers(registry)
        for index in range(12):
            provider_id = f"large{index:02d}"
            facts = tuple(
                StatusFact(name=f"fact{fact_index:02d}", value="v" * 256, description="d" * 512)
                for fact_index in range(32)
            )
            component = ComponentStatus(
                id=provider_id,
                display_name="D" * 128,
                state=ComponentState.IDLE,
                capabilities=tuple(f"cap{index:02d}.{cap:03d}" for cap in range(128)),
                summary="S" * 512,
                facts=facts,
                warnings=tuple(f"warning{warning:02d}" for warning in range(32)),
                observed_at=utc_now(),
            )
            registry.register(_ResultProvider(provider_id, component))
        service = ProjectStatusService(registry, Path("C:/workspace"))
        first = await service.status()
        second = await service.status()
        assert first.response_truncated is True
        assert first.partial is True
        assert first.omitted_components
        assert first.omitted_components == second.omitted_components
        assert len(first.model_dump_json().encode("utf-8")) <= MAX_STATUS_JSON_BYTES
        assert tuple(item.id for item in first.components) == tuple(
            sorted(item.id for item in first.components)
        )

    asyncio.run(exercise())


def test_many_external_plugin_capabilities_truncate_without_invalidating_manager(
    tmp_path: Path,
) -> None:
    class CapabilityHeavyPlugin(ForgePlugin):
        def __init__(self) -> None:
            super().__init__(
                PluginMetadata(
                    plugin_id="capability_heavy",
                    provides=frozenset(
                        {f"external.capability{index:03d}" for index in range(160)}
                        | {"INVALID CAPABILITY"}
                    ),
                )
            )

        async def start(self, context: PluginContext) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def exercise() -> None:
        application = ForgeApplication.create(
            ForgeConfig(workspace_root=tmp_path),
            builtin_plugins=(CapabilityHeavyPlugin(),),
        )
        await application.start()
        status = await application.services.get("project_status_service").status()
        manager = next(item for item in status.components if item.id == "plugin_manager")
        assert manager.state is ComponentState.AVAILABLE
        assert "capabilities_truncated" in manager.warnings
        assert len(manager.capabilities) == 128
        assert "INVALID CAPABILITY" not in manager.capabilities
        assert status.response_truncated is True
        assert status.partial is True
        assert len(status.capabilities) == 128
        await application.aclose()

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
        # Single-flight shields the shared snapshot from one cancelled client.
        assert provider.cancelled is False
        await registry.aclose()
        assert provider.cancelled is True
        assert not registry._active_tasks
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
    ("components", "health", "activity"),
    [
        (("core", ComponentState.FAILED), ProjectHealth.FAILED, ProjectActivity.IDLE),
        (("workspace", ComponentState.FAILED), ProjectHealth.FAILED, ProjectActivity.IDLE),
        (("process_runtime", ComponentState.FAILED), ProjectHealth.FAILED, ProjectActivity.IDLE),
        (("plugin_manager", ComponentState.FAILED), ProjectHealth.FAILED, ProjectActivity.IDLE),
        (("clangd", ComponentState.UNAVAILABLE), ProjectHealth.HEALTHY, ProjectActivity.IDLE),
        (("clangd", ComponentState.DEGRADED), ProjectHealth.DEGRADED, ProjectActivity.IDLE),
        (("clangd", ComponentState.FAILED), ProjectHealth.DEGRADED, ProjectActivity.IDLE),
        (("debugger", ComponentState.FAILED), ProjectHealth.DEGRADED, ProjectActivity.IDLE),
        (("debugger", ComponentState.ACTIVE), ProjectHealth.HEALTHY, ProjectActivity.BUSY),
        (("debugger", ComponentState.PAUSED), ProjectHealth.HEALTHY, ProjectActivity.PAUSED),
        (("debugger", ComponentState.STOPPED), ProjectHealth.HEALTHY, ProjectActivity.IDLE),
        (("cmake", ComponentState.ACTIVE), ProjectHealth.HEALTHY, ProjectActivity.BUSY),
        (("quality", ComponentState.ACTIVE), ProjectHealth.HEALTHY, ProjectActivity.BUSY),
        (("clangd", ComponentState.STARTING), ProjectHealth.HEALTHY, ProjectActivity.BUSY),
    ],
)
def test_health_and_activity_rules(components, health, activity) -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        _register_health_providers(registry, components)
        status = await ProjectStatusService(registry, Path("C:/workspace")).status()
        assert status.health is health
        assert status.activity is activity

    asyncio.run(exercise())


def test_optional_timeout_is_degraded_partial_and_critical_absence_is_failed() -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        _register_health_providers(registry)
        registry.register(_Provider("optional", delay=10))
        timed_out = await ProjectStatusService(
            registry,
            Path("C:/workspace"),
            provider_timeout_seconds=0.01,
            total_timeout_seconds=0.1,
        ).status()
        assert timed_out.health is ProjectHealth.DEGRADED
        assert timed_out.partial is True
        assert timed_out.timed_out_components == ("optional",)

        missing = ProjectStatusRegistry()
        for provider_id in ("core", "workspace", "process_runtime"):
            missing.register(_Provider(provider_id))
        absent = await ProjectStatusService(missing, Path("C:/workspace")).status()
        assert absent.health is ProjectHealth.FAILED
        assert absent.partial is True
        assert "critical_component_missing:plugin_manager" in absent.warnings

    asyncio.run(exercise())


def test_paused_precedes_busy_and_operation_warnings_do_not_fail_services() -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        _register_health_providers(registry)
        registry.register(_Provider("debugger", state=ComponentState.PAUSED))
        registry.register(
            _Provider(
                "cmake",
                state=ComponentState.ACTIVE,
                warnings=("last_build_failure", "last_test_failure"),
                capabilities=("shared", "cmake.build"),
            )
        )
        registry.register(
            _Provider(
                "quality",
                state=ComponentState.IDLE,
                warnings=("last_format_failure", "last_tidy_failure"),
                capabilities=("shared", "quality.tidy"),
            )
        )
        status = await ProjectStatusService(registry, Path("C:/workspace")).status()
        assert status.health is ProjectHealth.HEALTHY
        assert status.activity is ProjectActivity.PAUSED
        assert status.capabilities == ("cmake.build", "quality.tidy", "shared")
        assert len(status.capabilities) == len(set(status.capabilities))
        assert {
            "last_build_failure", "last_test_failure",
            "last_format_failure", "last_tidy_failure",
        } <= set(status.warnings)

    asyncio.run(exercise())


def test_health_and_ordering_do_not_depend_on_registration_order() -> None:
    async def make(reverse: bool):
        registry = ProjectStatusRegistry()
        entries = [
            ("core", ComponentState.ACTIVE),
            ("workspace", ComponentState.AVAILABLE),
            ("process_runtime", ComponentState.AVAILABLE),
            ("plugin_manager", ComponentState.AVAILABLE),
            ("clangd", ComponentState.FAILED),
            ("debugger", ComponentState.PAUSED),
        ]
        for provider_id, state in reversed(entries) if reverse else entries:
            registry.register(_Provider(provider_id, state=state, capabilities=("shared", provider_id)))
        return await ProjectStatusService(registry, Path("C:/workspace")).status()

    async def exercise() -> None:
        first, second = await asyncio.gather(make(False), make(True))
        assert first.health is second.health is ProjectHealth.DEGRADED
        assert first.activity is second.activity is ProjectActivity.PAUSED
        assert tuple((item.id, item.state, item.capabilities) for item in first.components) == tuple(
            (item.id, item.state, item.capabilities) for item in second.components
        )
        assert first.capabilities == second.capabilities

    asyncio.run(exercise())


def test_failed_build_warning_does_not_make_cmake_service_failed() -> None:
    async def exercise() -> None:
        registry = ProjectStatusRegistry()
        _register_health_providers(registry, ("cmake", ComponentState.IDLE))
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
        plugins = application.services.get("plugins")
        cmake = plugins._records["cmake"].plugin.service
        clangd = plugins._records["clangd"].plugin.service
        debugger = plugins._records["debugger"].plugin.service
        quality = plugins._records["quality"].plugin

        async def forbidden_process(*args, **kwargs):
            raise AssertionError("project status must not run processes")

        def forbidden_sync(*args, **kwargs):
            raise AssertionError("project status invoked a forbidden side effect")

        with monkeypatch.context() as audit:
            for method in (
                "run", "start", "run_trusted_adapter", "start_trusted_adapter",
            ):
                audit.setattr(runtime, method, forbidden_process)
            audit.setattr(runtime, "resolve_executable", forbidden_sync)
            for method in (
                "read_text", "list_files", "get_snapshot", "require_directory",
                "open_generated_directory", "validate_reported_path", "validate_execution_path",
                "apply_unified_patch", "apply_text_edits",
            ):
                audit.setattr(workspace, method, forbidden_sync)
            for method in (
                "status", "list_presets", "configure", "list_targets", "build",
                "list_tests", "run_tests", "_load_codemodel",
            ):
                audit.setattr(cmake, method, forbidden_process if method in {
                    "status", "list_presets", "configure", "build", "list_tests", "run_tests"
                } else forbidden_sync)
            for method in (
                "status", "start", "diagnostics", "hover", "_request", "_notify",
                "_synchronize_document",
            ):
                audit.setattr(clangd, method, forbidden_process)
            for method in (
                "status", "list_adapters", "events", "launch", "stop", "threads",
                "stack_trace", "scopes", "variables", "evaluate", "_request",
            ):
                audit.setattr(debugger, method, forbidden_process)
            for tool in (quality.clang_format, quality.clang_tidy):
                for method in ("status", "check", "apply", "list_checks", "run"):
                    if hasattr(tool, method):
                        audit.setattr(tool, method, forbidden_process)
            audit.setattr(quality.sanitizer, "parse", forbidden_sync)
            audit.setattr(plugins, "start", forbidden_process)
            audit.setattr(plugins, "aclose", forbidden_process)

            service = application.services.get("project_status_service")
            clangd._failure = "C:/secret/source.cpp token=clangd-secret"
            debugger._failure = "stack variable evaluate output debugger-secret"
            plugins._records["quality"].error = "C:/external/plugin/entry_point.py"
            cmake._configured_binary_dir = "long-cmake-path-" + "x" * 300
            clangd._compile_commands_dir = "long-clangd-path-" + "y" * 300
            first, second = await asyncio.gather(service.status(), service.status())
            assert len(first.components) == len(second.components) == 8
            assert first.partial is False
            assert first.generated_at.tzinfo is not None
            assert first.generated_at.utcoffset() == timedelta(0)
            assert all(component.observed_at.tzinfo is not None for component in first.components)
            assert all(
                component.observed_at <= first.generated_at + timedelta(seconds=5)
                for component in first.components
            )
            serialized = first.model_dump_json().casefold()
            for forbidden in (
                "clangd-secret", "debugger-secret", "entry_point.py", "argv",
                "environment", "executable_path", "process_handle", "password=",
                "replacement_text", "stack variable", "evaluate output",
                "long-cmake-path", "long-clangd-path",
            ):
                assert forbidden not in serialized
            assert "workspace_relative_path_omitted" in first.warnings

            cmake._active_operations = 1
            quality._active_operations = 1
            clangd._state = ClangdSessionState.STARTING
            async with debugger._state_lock:
                debugger._state = DebuggerState.PAUSED
            active = await service.status()
            assert active.activity is ProjectActivity.PAUSED
            cmake._active_operations = 0
            quality._active_operations = 0
            clangd._state = ClangdSessionState.STOPPED
            async with debugger._state_lock:
                debugger._state = DebuggerState.STOPPED
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
        first_status = await first.services.get("project_status_service").status()
        second_status = await second.services.get("project_status_service").status()
        assert first_status.workspace_root == second_status.workspace_root == "configured"
        assert str(first_root) not in first_status.model_dump_json()
        assert str(second_root) not in second_status.model_dump_json()
        await first.aclose()
        await second.aclose()

    asyncio.run(exercise())


def test_plugin_start_failure_unregisters_provider_registered_before_failure(tmp_path: Path) -> None:
    class FailingAfterRegistration(ForgePlugin):
        def __init__(self) -> None:
            super().__init__(
                PluginMetadata(
                    plugin_id="status_failure",
                    requires_services=("project_status_registry",),
                )
            )
            self.registry: ProjectStatusRegistry | None = None

        async def start(self, context: PluginContext) -> None:
            registry = context.services.get("project_status_registry")
            assert isinstance(registry, ProjectStatusRegistry)
            self.registry = registry
            registry.register(_Provider("registered_then_failed"))
            raise RuntimeError("secret startup text")

        async def stop(self) -> None:
            if self.registry is not None:
                self.registry.unregister("registered_then_failed")
                self.registry = None

    async def exercise() -> None:
        application = ForgeApplication.create(
            ForgeConfig(workspace_root=tmp_path),
            builtin_plugins=(FailingAfterRegistration(),),
        )
        with pytest.raises(Exception, match="status_failure"):
            await application.start()
        registry = application.services.get("project_status_registry")
        assert "registered_then_failed" not in registry.provider_ids()
        await application.aclose()

    asyncio.run(exercise())


def test_registry_closes_before_feature_provider_shutdown(tmp_path: Path) -> None:
    class ObservingPlugin(ForgePlugin):
        def __init__(self) -> None:
            super().__init__(
                PluginMetadata(
                    plugin_id="status_shutdown_order",
                    requires_services=("project_status_registry",),
                )
            )
            self.registry: ProjectStatusRegistry | None = None
            self.saw_closed_registry = False

        async def start(self, context: PluginContext) -> None:
            registry = context.services.get("project_status_registry")
            assert isinstance(registry, ProjectStatusRegistry)
            self.registry = registry
            registry.register(_Provider("shutdown_order_component"))

        async def stop(self) -> None:
            assert self.registry is not None
            self.saw_closed_registry = self.registry.closed
            self.registry.unregister("shutdown_order_component")

    async def exercise() -> None:
        plugin = ObservingPlugin()
        application = ForgeApplication.create(
            ForgeConfig(workspace_root=tmp_path), builtin_plugins=(plugin,)
        )
        await application.start()
        await application.aclose()
        assert plugin.saw_closed_registry is True
        assert "shutdown_order_component" not in plugin.registry.provider_ids()

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
        cmake_facts = {fact.name: fact.value for fact in cmake_completed.facts}
        assert cmake_facts["last_build_duration"] >= 0
        assert type(cmake_facts["last_build_exit_code"]) is int
        assert completed.health is ProjectHealth.HEALTHY
        serialized = completed.model_dump_json()
        assert "compiler output" not in serialized
        assert "diagnostic text" not in serialized
        await application.aclose()

    asyncio.run(exercise())


def test_cmake_concurrent_operation_counter_and_cancellation_are_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        (tmp_path / "build").mkdir()
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        await application.start()
        plugins = application.services.get("plugins")
        cmake = plugins._records["cmake"].plugin.service
        runtime = application.services.get("process_runtime")
        entered_count = 0
        both_entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_build(*args, **kwargs):
            nonlocal entered_count
            entered_count += 1
            if entered_count == 2:
                both_entered.set()
            await release.wait()
            now = datetime.now(UTC)
            return ProcessResult(
                exit_code=0,
                started_at=now,
                finished_at=now,
                    stdout=ProcessOutput(text=""),
                    stderr=ProcessOutput(text=""),
            )

        monkeypatch.setattr(runtime, "run", blocked_build)
        first = asyncio.create_task(cmake.build(binary_dir="build"))
        second = asyncio.create_task(cmake.build(binary_dir="build"))
        await both_entered.wait()
        active = await application.services.get("project_status_service").status()
        cmake_active = next(item for item in active.components if item.id == "cmake")
        assert {fact.name: fact.value for fact in cmake_active.facts}["active_operations"] == 2
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        one_active = await application.services.get("project_status_service").status()
        assert {fact.name: fact.value for fact in next(
            item for item in one_active.components if item.id == "cmake"
        ).facts}["active_operations"] == 1
        release.set()
        await second
        completed = await application.services.get("project_status_service").status()
        facts = {fact.name: fact.value for fact in next(
            item for item in completed.components if item.id == "cmake"
        ).facts}
        assert facts["active_operations"] == 0
        assert facts["last_build_outcome"] == "success"
        assert facts["last_build_duration"] >= 0
        await application.aclose()

    asyncio.run(exercise())


def test_quality_concurrent_counter_and_cancellation_do_not_stick_busy(
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
        entered_count = 0
        both_entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_check(paths):
            nonlocal entered_count
            entered_count += 1
            if entered_count == 2:
                both_entered.set()
            await release.wait()
            raise QualityToolExecutionError("safe failure")

        monkeypatch.setattr(quality.clang_format, "check", blocked_check)
        first = asyncio.create_task(contribution.handler({"paths": ["a.cpp"]}))
        second = asyncio.create_task(contribution.handler({"paths": ["b.cpp"]}))
        await both_entered.wait()
        active = await application.services.get("project_status_service").status()
        facts = {fact.name: fact.value for fact in next(
            item for item in active.components if item.id == "quality"
        ).facts}
        assert facts["active_operations"] == 2
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        response = await second
        assert response["error"]["code"] == "quality_tool_execution_error"
        completed = await application.services.get("project_status_service").status()
        component = next(item for item in completed.components if item.id == "quality")
        facts = {fact.name: fact.value for fact in component.facts}
        assert facts["active_operations"] == 0
        assert facts["last_format_duration"] >= 0
        assert "last_format_failure" in component.warnings
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
        clangd._state = ClangdSessionState.RUNNING
        clangd_active = await service.status()
        assert next(item for item in clangd_active.components if item.id == "clangd").state is ComponentState.ACTIVE
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
        async with debugger._state_lock:
            debugger._state = DebuggerState.TERMINATED
        terminal = await service.status()
        assert terminal.activity is ProjectActivity.IDLE
        assert next(item for item in terminal.components if item.id == "debugger").state is ComponentState.STOPPED
        await application.aclose()

    asyncio.run(exercise())


def test_clangd_cached_counter_work_is_bounded_and_marked_truncated(tmp_path: Path) -> None:
    async def exercise() -> None:
        application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
        await application.start()
        plugins = application.services.get("plugins")
        clangd = plugins._records["clangd"].plugin.service
        clangd._documents = {
            f"file{index}.cpp": SimpleNamespace(diagnostics=(), stale_diagnostics=False)
            for index in range(1_000)
        }
        status = await application.services.get("project_status_service").status()
        component = next(item for item in status.components if item.id == "clangd")
        facts = {fact.name: fact.value for fact in component.facts}
        assert facts["open_documents"] == 1_000
        assert facts["diagnostic_counts_truncated"] is True
        assert "cached_counts_truncated" in component.warnings
        assert component.stale is True
        clangd._documents.clear()
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
        completed = await application.services.get("project_status_service").status()
        quality_component = next(item for item in completed.components if item.id == "quality")
        quality_facts = {fact.name: fact.value for fact in quality_component.facts}
        assert quality_facts["active_operations"] == 0
        assert quality_facts["last_format_duration"] >= 0
        assert "last_format_failure" in quality_component.warnings
        await application.aclose()

    asyncio.run(exercise())
