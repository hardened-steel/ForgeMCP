import pytest

from forgemcp.toolchain.loader import load_tools
from forgemcp.toolchain.spec import ToolOutput


@pytest.mark.anyio
@pytest.mark.parametrize("name,output,version", [
    ("cmake", "cmake version 4.1.2\n", "4.1.2"),
    ("ctest", "ctest version 4.1.2\n", "4.1.2"),
    ("ninja", "1.13.1\n", "1.13.1"),
    ("make", "GNU Make 4.4.1\n", "4.4.1"),
    ("msbuild", "18.0.1.234\n", "18.0.1.234"),
    ("cl", "Microsoft (R) C/C++ Optimizing Compiler Version 19.50.35717 for x64\n", "19.50.35717"),
    ("cl", "Оптимизирующий компилятор Microsoft (R) C/C++ версии 19.44.35221 для x64\n", "19.44.35221"),
    ("link", "Microsoft (R) Incremental Linker Version 14.50.35717.0\n", "14.50.35717.0"),
    ("clang", "Ubuntu clang version 20.1.2\n", "20.1.2"),
    ("clang++", "Apple clang version 17.0.0 (clang-1700.0.13.5)\n", "17.0.0"),
    ("clang-cl", "clang version 22.1.0\n", "22.1.0"),
    ("gcc", "14.2.0\n", "14.2.0"),
    ("g++", "15.1.0\n", "15.1.0"),
    ("lld", "LLD 20.1.2 (compatible with GNU linkers)\n", "20.1.2"),
    ("lld-link", "LLD 20.1.2\n", "20.1.2"),
    ("clangd", "clangd version 20.1.2\n", "20.1.2"),
    ("git", "git version 2.49.0.windows.1\n", "2.49.0.windows.1"),
    ("lldb-dap", "lldb version 20.1.2\n", "20.1.2"),
    ("gdb", "GNU gdb (Ubuntu 15.1-1ubuntu2) 15.1\n", "15.1"),
])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_versions_survive_chunk_boundaries(name, output, version, stream):
    spec = next(spec for spec in load_tools() if spec.name == name)
    async def chunks():
        for char in output:
            yield ToolOutput(stream, char)
    async def emit(event):
        pytest.fail("Version parsers need no events")
    assert await spec.parsers["version"](chunks(), emit) == version


@pytest.mark.anyio
async def test_unrecognized_version_is_none():
    async def chunks():
        yield ToolOutput("stdout", "an unfamiliar banner")
    async def emit(event):
        pass
    spec = next(spec for spec in load_tools() if spec.name == "cmake")
    assert await spec.parsers["version"](chunks(), emit) is None
