import asyncio

from forgemcp.core.application import ForgeApplication, LifecycleState
from forgemcp.core.config import ForgeConfig
from forgemcp.server import create_server, server_status


def test_server_status_smoke_test(tmp_path):
    application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
    application.start()

    status = server_status(application)

    assert status["workspace_root"] == str(tmp_path.resolve())
    assert status["state"] == LifecycleState.RUNNING.value
    assert status["services"] == ["config", "logger", "process_runtime", "workspace"]
    server = create_server(application)
    assert server.name == "ForgeMCP"
    assert [tool.name for tool in asyncio.run(server.list_tools())] == ["server_status"]
