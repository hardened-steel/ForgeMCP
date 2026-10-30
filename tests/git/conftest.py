"""Real local Git repositories isolated in copies of the acceptance project."""

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from shutil import which

import pytest

from forgemcp.process.models import ProcessEncoding, ProcessTimeout
from forgemcp.process.service import ProcessService
from forgemcp.server import ServerRuntime, create_runtime
from forgemcp.toolchain.spec import Toolset
from forgemcp.toolchain.tools import git


@dataclass
class GitProject:
    """Run setup commands and compose a server against one disposable repository."""

    root: Path
    temporary: Path
    executable: Path
    processes: ProcessService

    async def command(
        self,
        *arguments: str,
        cwd: Path | None = None,
        success: bool = True,
    ) -> str:
        """Execute a bounded setup or verification command outside the public MCP surface."""
        chunks = {"stdout": [], "stderr": []}
        async with await self.processes.launch(
            self.executable,
            arguments,
            cwd=cwd or self.root,
            encoding=ProcessEncoding("utf-8", "strict"),
            timeout=ProcessTimeout(total=20),
            env={"GIT_TERMINAL_PROMPT": "0"},
        ) as session:
            await session.close_stdin()
            async for chunk in session.output():
                chunks[chunk.stream].append(chunk.text)
            code = await session.wait()
        if success:
            assert code == 0, "".join(chunks["stderr"])
        return "".join(chunks["stdout"])

    async def commit(self, message: str) -> str:
        """Commit all disposable project changes and return the complete commit identifier."""
        await self.command("add", "--all")
        await self.command("commit", "--message", message)
        return (await self.command("rev-parse", "HEAD")).strip()

    def runtime(self, project: Path | None = None) -> ServerRuntime:
        """Bind the real Git executable without probing host compilers or Visual Studio."""
        runtime = create_runtime(
            project or self.root,
            storage_root=self.temporary / f"storage-{(project or self.root).name}",
            progress_interval=0,
        )
        runtime.toolchains.toolsets = (
            Toolset(
                "system",
                "System",
                (git.create_spec(self.executable, runtime.processes),),
                None,
                True,
            ),
        )
        runtime.toolchains.initialized = True
        return runtime


@pytest.fixture(name="anyio_backend")
def asyncio_backend() -> str:
    """Use the async process implementation's asyncio backend."""
    return "asyncio"


@pytest.fixture
async def git_project(
    cpp_acceptance_project: Path,
    tmp_path: Path,
    anyio_backend: str,
) -> AsyncGenerator[GitProject, None]:
    """Initialize a function-scoped fixture repository with deterministic local configuration."""
    del anyio_backend
    executable = which("git")
    if executable is None:
        pytest.skip("A Git executable is required for local workflow tests.")
    processes = ProcessService(tmp_path)
    project = GitProject(cpp_acceptance_project, tmp_path, Path(executable), processes)
    try:
        await project.command("init", "--initial-branch=main")
        for name, value in (
            ("user.name", "ForgeMCP Test"),
            ("user.email", "forgemcp@example.invalid"),
            ("core.autocrlf", "false"),
            ("core.safecrlf", "false"),
            ("commit.gpgsign", "false"),
            ("tag.gpgsign", "false"),
            ("core.hooksPath", ".git/hooks"),
            ("protocol.file.allow", "always"),
        ):
            await project.command("config", "--local", name, value)
        await project.commit("Initial acceptance project")
        yield project
    finally:
        await processes.close()
