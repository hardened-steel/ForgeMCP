import asyncio
import sys

from forgemcp.config import ForgeConfig
from forgemcp.processes import ProcessManager
from forgemcp.workspace import WorkspaceService


def test_process_manager_captures_output(tmp_path):
    config = ForgeConfig(workspace_root=tmp_path)
    manager = ProcessManager(config, WorkspaceService(config))

    result = asyncio.run(manager.run([sys.executable, "-c", "print('ready')"]))

    assert result.exit_code == 0
    assert result.stdout.strip() == "ready"
    assert result.timed_out is False


def test_process_manager_times_out(tmp_path):
    config = ForgeConfig(workspace_root=tmp_path, process_timeout_seconds=0.01)
    manager = ProcessManager(config, WorkspaceService(config))

    result = asyncio.run(manager.run([sys.executable, "-c", "import time; time.sleep(1)"]))

    assert result.timed_out is True


def test_process_manager_reports_lifecycle_progress(tmp_path):
    config = ForgeConfig(workspace_root=tmp_path)
    manager = ProcessManager(config, WorkspaceService(config))
    updates = []

    async def report(update):
        updates.append(update)

    asyncio.run(manager.run([sys.executable, "-c", "pass"], progress_reporter=report))

    assert updates[0].completed == 0
    assert updates[-1].completed == 1
