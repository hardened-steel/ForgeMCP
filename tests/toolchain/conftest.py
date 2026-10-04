import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def scripted_processes(cpp_acceptance_project):
    """Exercise tool methods with real Python children, without installed tool dependencies."""
    import asyncio
    import sys
    from forgemcp.process.service import ProcessService

    class ScriptedProcesses(ProcessService):
        code = "print('cmake version 4.1.2')"
        timeout = None

        def __init__(self, root):
            super().__init__(root)
            self.calls = []
            self.started = asyncio.Event()

        async def launch(self, executable, arguments=(), **kwargs):
            self.calls.append((executable, arguments, kwargs.copy()))
            if self.timeout is not None:
                kwargs["timeout"] = self.timeout
            session = await super().launch(
                sys.executable,
                ("-u", "-c", self.code),
                **kwargs,
            )
            self.started.set()
            return session

    service = ScriptedProcesses(cpp_acceptance_project)
    async with asyncio.timeout(15):
        try:
            yield service
        finally:
            await service.close()
