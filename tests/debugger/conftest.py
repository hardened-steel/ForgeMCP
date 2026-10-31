"""Build isolated native fixtures for explicitly requested real-adapter checks."""

from collections.abc import AsyncGenerator
import os
from pathlib import Path
import shutil

import pytest

from forgemcp.process.models import ProcessTimeout
from forgemcp.process.service import ProcessService
from forgemcp.toolchain.service import ToolchainService


@pytest.fixture
async def real_debugger_project(
    cpp_acceptance_project: Path,
) -> AsyncGenerator[tuple[Path, ProcessService, Path, Path], None]:
    """Compile the complete acceptance copy and close all managed programs afterward."""
    tools = {name: shutil.which(name) for name in ("lldb-dap", "clang++", "cmake", "ninja")}
    if not all(tools.values()):
        pytest.skip("A complete LLVM, CMake, and Ninja installation is required.")
    root = cpp_acceptance_project
    processes = ProcessService(root)
    try:
        environment = None
        if os.name == "nt":
            toolchains = ToolchainService(processes)
            await toolchains.initialize()
            environment = next(
                (
                    item.environment for item in toolchains.list_toolsets()
                    if item.environment and any(tool.name == "cl" for tool in item.tools)
                ),
                None,
            )
            if environment is None:
                pytest.skip("Windows fixture builds require an installed MSVC environment.")
        build = root / "build" / "debugger-real"
        commands = [
            [
                "-S",
                str(root),
                "-B",
                str(build),
                "-G",
                "Ninja",
                "-DCMAKE_BUILD_TYPE=Debug",
                "-DFIXTURE_LLVM_DWARF=ON",
                f"-DCMAKE_CXX_COMPILER={tools['clang++']}",
                f"-DCMAKE_MAKE_PROGRAM={tools['ninja']}",
            ],
            [
                "--build",
                str(build),
                "--target",
                "fixture_debug",
            ],
        ]
        for arguments in commands:
            async with await processes.launch(
                tools["cmake"],
                arguments,
                env=environment,
                inherit_environment=environment is None,
                timeout=ProcessTimeout(total=120),
            ) as command:
                await command.close_stdin()
                async for _ in command.output():
                    pass
                assert await command.wait() == 0, command.process_id
        executable = build / ("fixture_debug.exe" if os.name == "nt" else "fixture_debug")
        yield root, processes, executable, Path(tools["lldb-dap"])
    finally:
        await processes.close()
