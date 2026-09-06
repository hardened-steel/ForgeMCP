# ForgeMCP

ForgeMCP is a Python MCP server for structured C and C++ development workflows.
The repository currently contains a deliberately small vertical slice that establishes
the conventions for future workspace, CMake, clangd, quality, and debugger modules.

## Current MCP surface

- Tool `workspace_overview` returns a typed, bounded summary of the configured
  workspace, reports progress, carries a packaged icon, and opens an MCP App widget.
- Resource template `forgemcp://workspace/files/{extension}` lists bounded,
  workspace-relative C/C++ file paths.
- Prompt `inspect_cpp_workspace` starts a focused, read-only project inspection.
- Completions suggest supported resource extensions and prompt focus values.
- Tool `processes_overview` shows running and completed external development tools.
- Resource template `forgemcp://processes/{process_id}` exposes the retained state and
  text transcript of one process.
- Tool `toolsets_list` lists every cached toolset; `toolset_get(toolset_id)` returns
  its absolute executable paths, kinds, and versions. Both open the toolsets widget.
- Resources `forgemcp://toolsets` and `forgemcp://toolsets/{toolset_id}` expose the
  same cached state as markdown, with completion for retained toolset IDs.

The tools remain useful in clients without MCP Apps support because the Python SDK
serializes their typed results into both text `content` and `structuredContent`.

ForgeMCP does not expose arbitrary command execution over MCP. Feature services use
the shared process service internally; its public MCP surface is read-only.

## Requirements

- Python 3.11 or newer
- Node.js `^20.19.0` or `>=22.12.0` for clean editable installs, wheel builds,
  and widget development

The server is built against MCP Python SDK `2.1.1`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Build

One command installs the locked frontend dependencies, builds every widget, and
creates the Python wheel with its HTML and icon assets:

```powershell
.\.venv\Scripts\python.exe -m build --wheel
```

The wheel is written to `dist/`. Generated HTML under `src/forgemcp/assets/` is build
output; change its source under `frontend/`, never the HTML directly.

## Run

Run against the current directory:

```powershell
.\.venv\Scripts\forgemcp.exe
```

Pass `--workspace` only when the target differs from the server process working
directory:

```powershell
.\.venv\Scripts\forgemcp.exe --workspace examples/cpp-acceptance-project
```

In VS Code, the `ForgeMCP: server` launch configuration is the one-button path: it
builds the wheel first and then starts the server for this repository.

All MCP traffic uses stdout. Operational logs must go to stderr.

## Toolchain discovery

Before serving requests, ForgeMCP discovers one System toolset from `PATH` and, on
Windows, a separate toolset for every Visual Studio instance reported by the standard
Installer `vswhere.exe`. Each VS toolset uses its own `VsDevCmd.bat` environment.
Toolsets can be incomplete or empty. ForgeMCP does not select a current or preferred
toolset; consumers must supply an explicit ID.

Add separate user toolsets with repeated `--toolset NAME TOOL=PATH ...` options:

```powershell
forgemcp `
  --toolset "LLVM 20" `
    "cmake=C:\Tools\CMake\bin\cmake.exe" `
    "clang=C:\LLVM20\bin\clang.exe" `
    "clang++=C:\LLVM20\bin\clang++.exe" `
    "clangd=C:\LLVM20\bin\clangd.exe" `
  --toolset "LLVM 22" `
    "clang++=D:\LLVM22\bin\clang++.exe" `
    "lldb-dap=D:\LLVM22\bin\lldb-dap.exe"
```

Names must be unique, and tool names must match built-in specs. Relative paths resolve
against the server working directory; paths inside the workspace are permitted.
Invalid explicit paths are configuration errors and never fall back to system tools.
User toolsets inherit the server environment. Their IDs are deterministic hashes of
their names, so changing paths keeps an ID stable.

Built-ins cover CMake, CTest, Ninja, Make, MSBuild, MSVC (`cl`), `link`, Clang,
`clang++`, `clang-cl`, GCC, `g++`, LLD, `lld-link`, clangd, Git, LLDB-DAP, GDB, and
`cppvsdbg`. The last adapter may be found as `OpenDebugAD7.exe` or VS Code's bundled
`vsdbg.exe`; it has no portable version probe. An unknown or failed version probe
leaves the discovered path available with a null version.

Discovery is cached for the server lifetime. List/get tools and resource reads never
repeat probes; restart the server to discover changed installations. All probes,
`vswhere`, and the command capturing `VsDevCmd` plus `set` remain visible in the process
overview and retain their transcripts. Toolset resources omit environments; the
environment capture's stdout is retained in its ordinary process transcript.

## Tests

Tests that need a C/C++ workspace use the shared `cpp_acceptance_project` fixture from
`tests/conftest.py`. It copies the complete acceptance project into a fresh pytest
temporary directory for each test, so tests can modify their workspace without
changing repository files.

## Verify

```powershell
.\.venv\Scripts\python.exe -m build --wheel
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

See [docs/architecture.md](docs/architecture.md) for module boundaries and the
registration lifecycle. Repository rules for coding agents live in [AGENTS.md](AGENTS.md).
