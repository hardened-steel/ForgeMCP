"""Unit tests for transport-neutral feature plugin composition."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError, dataclass

import pytest

from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.core.services import ServiceRegistry
from forgemcp.plugins import (
    DuplicateCapabilityError,
    DuplicatePluginIdError,
    DuplicateToolNameError,
    ForgePlugin,
    MissingPluginDependencyError,
    MissingRequiredServiceError,
    PluginContext,
    PluginDependencyCycleError,
    PluginApiVersionError,
    PluginManager,
    PluginMetadata,
    PluginStartError,
    PluginState,
    ToolContribution,
    ToolRegistry,
)


class RecordingPlugin(ForgePlugin):
    """A minimal plugin whose lifecycle calls are observable in a shared list."""

    def __init__(
        self,
        plugin_id: str,
        events: list[str],
        *,
        requires: tuple[str, ...] = (),
        requires_services: tuple[str, ...] = (),
        provides: frozenset[str] = frozenset(),
        api_version: str = "1",
        fail_start: bool = False,
        register_tool: bool = False,
    ) -> None:
        super().__init__(
            PluginMetadata(
                plugin_id=plugin_id,
                api_version=api_version,
                requires=requires,
                requires_services=requires_services,
                provides=provides,
            )
        )
        self._events = events
        self._fail_start = fail_start
        self._register_tool = register_tool
        self.context: PluginContext | None = None

    async def start(self, context: PluginContext) -> None:
        self.context = context
        self._events.append(f"start:{self.metadata.plugin_id}")
        if self._register_tool:
            context.tools.register(
                ToolContribution(name="inspect", description="Inspect a configured feature.", handler=lambda _: {})
            )
        if self._fail_start:
            raise RuntimeError("expected startup failure")

    async def stop(self) -> None:
        self._events.append(f"stop:{self.metadata.plugin_id}")


def manager_for(root, *, external_plugins_enabled: bool = False, allowlist: frozenset[str] = frozenset()):
    config = ForgeConfig(
        workspace_root=root,
        external_plugins_enabled=external_plugins_enabled,
        external_plugin_allowlist=allowlist,
    )
    services = ServiceRegistry()
    services.register("config", config)
    services.register("logger", create_logger("CRITICAL"))
    services.register("workspace", object())
    services.register("process_runtime", object())
    manager = PluginManager(config=config, services=services, logger=services.get("logger"))
    services.register("plugins", manager)
    return manager, config


def test_plugins_start_topologically_and_stop_in_reverse_order(tmp_path):
    manager, config = manager_for(tmp_path)
    events: list[str] = []
    gamma = RecordingPlugin("gamma", events, requires=("alpha",), requires_services=("workspace",))
    manager.register_builtin(gamma)
    manager.register_builtin(RecordingPlugin("beta", events))
    manager.register_builtin(RecordingPlugin("alpha", events))

    asyncio.run(manager.start())

    assert events == ["start:alpha", "start:beta", "start:gamma"]
    assert gamma.context is not None
    assert gamma.context.config is config
    assert gamma.context.services.names() == ("workspace",)
    assert not hasattr(gamma.context.services, "register")
    assert gamma.context.services.get("workspace") is not None
    with pytest.raises(KeyError, match="did not declare"):
        gamma.context.services.get("process_runtime")
    assert [status.state for status in manager.statuses()] == [PluginState.RUNNING] * 3

    asyncio.run(manager.aclose())

    assert events == ["start:alpha", "start:beta", "start:gamma", "stop:gamma", "stop:beta", "stop:alpha"]
    assert [status.state for status in manager.statuses()] == [PluginState.STOPPED] * 3


def test_plugin_manager_rejects_missing_plugin_and_service_dependencies(tmp_path):
    missing_dependency, _ = manager_for(tmp_path)
    missing_dependency.register_builtin(RecordingPlugin("cmake", [], requires=("workspace-tools",)))
    with pytest.raises(MissingPluginDependencyError, match="workspace-tools"):
        asyncio.run(missing_dependency.start())

    missing_service, _ = manager_for(tmp_path)
    missing_service.register_builtin(RecordingPlugin("clangd", [], requires_services=("index",)))
    with pytest.raises(MissingRequiredServiceError, match="index"):
        asyncio.run(missing_service.start())


def test_plugin_manager_rejects_dependency_cycles(tmp_path):
    manager, _ = manager_for(tmp_path)
    manager.register_builtin(RecordingPlugin("alpha", [], requires=("beta",)))
    manager.register_builtin(RecordingPlugin("beta", [], requires=("alpha",)))

    with pytest.raises(PluginDependencyCycleError, match="alpha, beta"):
        asyncio.run(manager.start())


def test_plugin_manager_rejects_duplicate_ids_and_capabilities(tmp_path):
    manager, _ = manager_for(tmp_path)
    manager.register_builtin(RecordingPlugin("cmake", []))
    with pytest.raises(DuplicatePluginIdError, match="cmake"):
        manager.register_builtin(RecordingPlugin("cmake", []))

    manager.register_builtin(RecordingPlugin("debugger", [], provides=frozenset({"debug.session"})))
    with pytest.raises(DuplicateCapabilityError, match="debug.session"):
        manager.register_builtin(RecordingPlugin("debug-ui", [], provides=frozenset({"debug.session"})))
    with pytest.raises(PluginApiVersionError, match="API version 2"):
        manager.register_builtin(RecordingPlugin("future", [], api_version="2"))


def test_plugin_metadata_is_immutable_and_copies_declared_dependencies():
    dependencies = ["workspace-tools"]
    metadata = PluginMetadata(
        plugin_id="cmake",
        requires=dependencies,
        provides={"cmake.configure"},
    )
    dependencies.append("other")

    assert metadata.requires == ("workspace-tools",)
    assert metadata.provides == frozenset({"cmake.configure"})
    with pytest.raises(FrozenInstanceError):
        metadata.plugin_id = "other"  # type: ignore[misc]


def test_tool_registry_uses_plugin_namespace_and_rejects_duplicates():
    registry = ToolRegistry()
    contribution = ToolContribution(name="configure", description="Configure a build tree.", handler=lambda _: {})

    registered = registry.register("cmake", contribution)

    assert registered.name == "cmake__configure"
    with pytest.raises(DuplicateToolNameError, match="cmake__configure"):
        registry.register("cmake", contribution)


def test_start_failure_rolls_back_already_started_plugins_and_tools(tmp_path):
    manager, _ = manager_for(tmp_path)
    events: list[str] = []
    manager.register_builtin(RecordingPlugin("alpha", events, register_tool=True))
    manager.register_builtin(RecordingPlugin("beta", events, requires=("alpha",), fail_start=True))

    with pytest.raises(PluginStartError, match="beta"):
        asyncio.run(manager.start())

    assert events == ["start:alpha", "start:beta", "stop:alpha"]
    assert manager.tools.contributions() == ()
    statuses = {status.plugin_id: status for status in manager.statuses()}
    assert statuses["alpha"].state is PluginState.STOPPED
    assert statuses["beta"].state is PluginState.FAILED
    asyncio.run(manager.aclose())
    assert events == ["start:alpha", "start:beta", "stop:alpha"]


@dataclass
class FakeEntryPoint:
    name: str
    plugin: object
    loaded: bool = False

    @property
    def value(self) -> str:
        return f"example:{self.name}"

    def load(self) -> object:
        self.loaded = True
        return self.plugin


class FakeEntryPoints(tuple):
    def select(self, *, group: str):
        assert group == "forgemcp.plugins"
        return self


def test_external_plugin_allowlist_never_imports_forbidden_entry_points(tmp_path, monkeypatch):
    events: list[str] = []
    allowed = FakeEntryPoint("allowed", RecordingPlugin("allowed", events))
    forbidden = FakeEntryPoint("forbidden", RecordingPlugin("forbidden", events))
    monkeypatch.setattr(
        "forgemcp.plugins.discovery.entry_points", lambda: FakeEntryPoints((forbidden, allowed))
    )
    manager, _ = manager_for(
        tmp_path, external_plugins_enabled=True, allowlist=frozenset({"allowed"})
    )

    asyncio.run(manager.start())

    assert allowed.loaded is True
    assert forbidden.loaded is False
    assert events == ["start:allowed"]
    asyncio.run(manager.aclose())


def test_disabled_external_discovery_does_not_even_read_entry_point_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "forgemcp.plugins.discovery.entry_points",
        lambda: pytest.fail("disabled discovery must not enumerate entry-point metadata"),
    )
    manager, _ = manager_for(tmp_path, allowlist=frozenset({"would-be-allowed"}))

    asyncio.run(manager.start())


def test_plugin_manager_aclose_is_idempotent(tmp_path):
    manager, _ = manager_for(tmp_path)
    events: list[str] = []
    manager.register_builtin(RecordingPlugin("cmake", events))
    asyncio.run(manager.start())

    asyncio.run(manager.aclose())
    asyncio.run(manager.aclose())

    assert events == ["start:cmake", "stop:cmake"]
