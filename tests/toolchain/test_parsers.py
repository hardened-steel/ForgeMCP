"""Version banner parsing across fragmented streams and unsupported version probes."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from forgemcp.process.models import ProcessOutput
from forgemcp.toolchain.errors import ToolParserError
from forgemcp.toolchain.loader import load_tools


@pytest.mark.anyio
@pytest.mark.parametrize(
    "name,output,version",
    [
        ("cmake", "cmake version 4.1.2\n", "4.1.2"),
        ("ctest", "ctest version 4.1.2\n", "4.1.2"),
        ("ninja", "1.13.1\n", "1.13.1"),
        ("make", "GNU Make 4.4.1\n", "4.4.1"),
        ("msbuild", "18.0.1.234\n", "18.0.1.234"),
        ("clang", "Ubuntu clang version 20.1.2\n", "20.1.2"),
        ("clang++", "Apple clang version 17.0.0 (clang-1700.0.13.5)\n", "17.0.0"),
        ("clang-cl", "clang version 22.1.0\n", "22.1.0"),
        ("gcc", "14.2.0\n", "14.2.0"),
        ("g++", "15.1.0\n", "15.1.0"),
        ("lld", "LLD 20.1.2 (compatible with GNU linkers)\n", "20.1.2"),
        ("lld-link", "LLD 20.1.2\n", "20.1.2"),
        ("clangd", "clangd version 20.1.2\n", "20.1.2"),
        ("git", "git version 2.49.0.windows.1\n", "2.49.0.windows.1"),
        ("gdb", "GNU gdb (Ubuntu 15.1-1ubuntu2) 15.1\n", "15.1"),
    ],
)
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_versions_survive_chunk_boundaries(name, output, version, stream):
    """Verify each version parser handles split stdout and stderr banners and closes its session."""
    info = next(info for info in load_tools() if info.name == name)
    session = Session([ProcessOutput(stream, char) for char in output])
    processes = SimpleNamespace(launch=AsyncMock(return_value=session))
    tool = info.create_spec(Path("tool.exe"), processes, None, True)
    assert await tool.methods["version"]() == version
    assert (
        session.consumed and session.waited and session.closed_stdin and session.exited
    )


class Session:
    """A scripted session recording output consumption, wait, and context cleanup."""

    def __init__(self, chunks):
        """Retain output chunks and reset the observable lifecycle flags."""
        self.chunks = chunks
        self.consumed = self.waited = self.closed_stdin = self.exited = False

    async def __aenter__(self):
        """Return the scripted session for a version query."""
        return self

    async def __aexit__(self, *args):
        """Record exit from the owned process context."""
        self.exited = True

    async def close_stdin(self):
        """Record that the version query closed process stdin."""
        self.closed_stdin = True

    async def output(self):
        """Yield scripted chunks and mark the stream fully consumed."""
        for chunk in self.chunks:
            yield chunk
        self.consumed = True

    async def wait(self):
        """Record the wait and return a successful exit code."""
        self.waited = True
        return 0


@pytest.mark.anyio
async def test_unrecognized_version_raises():
    """Verify unrecognized output raises a parser error after waiting and cleanup."""
    session = Session([ProcessOutput("stdout", "an unfamiliar banner")])
    processes = SimpleNamespace(launch=AsyncMock(return_value=session))
    info = next(info for info in load_tools() if info.name == "cmake")
    with pytest.raises(ToolParserError):
        await info.create_spec(Path("cmake"), processes, None, True).methods[
            "version"
        ]()
    assert session.exited and session.waited


@pytest.mark.parametrize("name", ["cl", "link", "cppvsdbg", "lldb-dap"])
def test_tools_without_version_have_empty_methods(name):
    """Verify tools lacking a portable version probe expose no bound operations."""
    info = next(info for info in load_tools() if info.name == name)
    processes = SimpleNamespace(launch=AsyncMock())
    assert info.create_spec(Path("tool"), processes, None, True).methods == {}
    processes.launch.assert_not_called()
