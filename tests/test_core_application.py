import asyncio

import pytest

from forgemcp.core.application import ForgeApplication, LifecycleState
from forgemcp.core.config import ForgeConfig
from forgemcp.core.errors import LifecycleError


def test_application_has_explicit_lifecycle_and_status(tmp_path):
    application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))

    assert application.status().state is LifecycleState.CREATED
    assert application.status().services == ("config", "logger", "process_runtime", "workspace")
    assert application.services.get("workspace").workspace_root == tmp_path.resolve()
    assert application.services.get("process_runtime").workspace_root == tmp_path.resolve()

    application.start()
    assert application.status().state is LifecycleState.RUNNING
    application.stop()
    assert application.status().state is LifecycleState.STOPPED

    with pytest.raises(LifecycleError):
        application.start()


def test_application_exposes_async_shutdown_for_process_services(tmp_path):
    application = ForgeApplication.create(ForgeConfig(workspace_root=tmp_path))

    asyncio.run(application.aclose())

    assert application.state is LifecycleState.STOPPED
