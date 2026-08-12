import asyncio

import pytest

from forgemcp.core.application import ForgeApplication, LifecycleState
from forgemcp.core.config import ForgeConfig
from forgemcp.core.errors import LifecycleError
from forgemcp.processes import ProcessRuntime
from forgemcp.plugins import ForgePlugin, PluginContext, PluginMetadata


def test_application_has_explicit_lifecycle_and_status(tmp_path):
    application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))

    assert application.status().state is LifecycleState.CREATED
    assert application.status().services == ("config", "logger", "plugins", "process_runtime", "workspace")
    assert application.services.get("workspace").workspace_root == tmp_path.resolve()
    assert application.services.get("process_runtime").workspace_root == tmp_path.resolve()

    asyncio.run(application.start())
    assert application.status().state is LifecycleState.RUNNING
    application.stop()
    assert application.status().state is LifecycleState.STOPPED

    with pytest.raises(LifecycleError):
        asyncio.run(application.start())


def test_application_exposes_async_shutdown_for_process_services(tmp_path):
    application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))

    asyncio.run(application.aclose())

    assert application.state is LifecycleState.STOPPED


def test_application_stops_plugins_before_the_process_runtime(tmp_path, monkeypatch):
    events: list[str] = []

    class RecordingPlugin(ForgePlugin):
        def __init__(self) -> None:
            super().__init__(PluginMetadata(plugin_id="integration"))

        async def start(self, context: PluginContext) -> None:
            events.append("plugin_start")

        async def stop(self) -> None:
            events.append("plugin_stop")

    original_aclose = ProcessRuntime.aclose

    async def record_runtime_close(runtime: ProcessRuntime) -> None:
        events.append("runtime_close")
        await original_aclose(runtime)

    monkeypatch.setattr(ProcessRuntime, "aclose", record_runtime_close)
    application = ForgeApplication.create(
        ForgeConfig(workspace_root=tmp_path), builtin_plugins=(RecordingPlugin(),)
    )

    asyncio.run(application.start())
    asyncio.run(application.aclose())

    assert events == ["plugin_start", "plugin_stop", "runtime_close"]
