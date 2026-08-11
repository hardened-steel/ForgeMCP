import asyncio

from forgemcp.config import ForgeConfig
from forgemcp.plugins import PluginContext, PluginRegistry
from forgemcp.processes import ProcessManager
from forgemcp.workspace import WorkspaceService


class FakePlugin:
    def __init__(self, plugin_id, requires=(), capabilities=()):
        self.id = plugin_id
        self.requires = requires
        self.capabilities = capabilities
        self.events = []

    async def start(self, context):
        self.events.append("start")

    async def stop(self):
        self.events.append("stop")


def test_registry_starts_dependencies_before_dependents(tmp_path):
    config = ForgeConfig(workspace_root=tmp_path)
    context = PluginContext(config, WorkspaceService(config), ProcessManager(config, WorkspaceService(config)))
    cmake = FakePlugin("cmake", capabilities=("build-system",))
    builder = FakePlugin("build", requires=("cmake",), capabilities=("build",))
    registry = PluginRegistry([builder, cmake])

    asyncio.run(registry.start_all(context))

    assert cmake.events == ["start"]
    assert builder.events == ["start"]
    assert registry.providers_for("build") == (builder,)

    asyncio.run(registry.stop_all())
    assert cmake.events == ["start", "stop"]
    assert builder.events == ["start", "stop"]
