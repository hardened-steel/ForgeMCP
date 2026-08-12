import asyncio
import sys
from pathlib import Path

import pytest

from forgemcp.core.application import ForgeApplication, LifecycleState
from forgemcp.core.config import ForgeConfig
from forgemcp.core.logging import create_logger
from forgemcp.core.services import ServiceRegistry
from forgemcp.processes import ProcessPolicy, ProcessRuntime
from forgemcp.server import create_server, server_status
from forgemcp.workspace import WorkspaceService


def test_server_status_smoke_test(tmp_path):
    application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))
    application.start()

    status = server_status(application)

    assert status["workspace_root"] == str(tmp_path.resolve())
    assert status["state"] == LifecycleState.RUNNING.value
    assert status["services"] == ["config", "logger", "process_runtime", "workspace"]
    server = create_server(lambda: application)
    assert server.name == "ForgeMCP"
    assert [tool.name for tool in asyncio.run(server.list_tools())] == ["server_status"]


def application_with_test_process_runtime(root: Path) -> ForgeApplication:
    """Compose an app whose process policy admits only this test interpreter."""
    config = ForgeConfig(workspace_root=root)
    logger = create_logger("CRITICAL")
    services = ServiceRegistry()
    services.register("config", config)
    services.register("logger", logger)
    services.register("workspace", WorkspaceService(config, logger))
    services.register(
        "process_runtime",
        ProcessRuntime(
            config,
            logger,
            policy=ProcessPolicy(
                allowed_executables=frozenset(),
                allowed_executable_paths=frozenset({Path(sys.executable).resolve()}),
                default_timeout_seconds=1.0,
                maximum_timeout_seconds=5.0,
                termination_grace_seconds=0.2,
            ),
        ),
    )
    return ForgeApplication(config, services)


def test_mcp_lifespan_closes_a_long_lived_process_after_an_exception(tmp_path):
    async def exercise() -> None:
        application = application_with_test_process_runtime(tmp_path)
        server = create_server(lambda: application)
        process_runtime = application.services.get("process_runtime")
        assert isinstance(process_runtime, ProcessRuntime)

        with pytest.raises(RuntimeError, match="lifespan failure"):
            async with server._mcp_server.lifespan(server._mcp_server):  # type: ignore[attr-defined]
                assert application.state is LifecycleState.RUNNING
                handle = await process_runtime.start(
                    [sys.executable, "-c", "import time; time.sleep(30)"]
                )
                await asyncio.sleep(0.05)
                raise RuntimeError("lifespan failure")

        assert application.state is LifecycleState.STOPPED
        assert handle.returncode is not None

    asyncio.run(exercise())
