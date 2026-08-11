# ForgeMCP

ForgeMCP is an MCP server that provides AI assistants with deep, structured integration for C++ development.

## Current MVP

The initial server exposes safe, read-only workspace tools over the MCP stdio transport:

- `workspace_info`
- `list_files`
- `read_file`

All file paths are resolved relative to `FORGEMCP_WORKSPACE` (or the process working directory if it is unset). Paths outside that root are rejected.

## Setup

Requires Python 3.11 or later.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

`mcp` is the only runtime dependency. To start the server for a C++ workspace:

```powershell
$env:FORGEMCP_WORKSPACE = "C:\path\to\cpp-project"
forgemcp
```

The next increments are patch-based file edits, CMake presets/build/test tools, then clangd and Debug Adapter Protocol integrations.

## Core structure

- `config.py` — typed runtime configuration and limits.
- `workspace.py` — the single safe filesystem boundary for all future plugins.
- `processes.py` — async, shell-free external process execution with timeouts and bounded output.
- `plugins.py` — contracts and lifecycle registry for pluggable CMake, clangd, debugger, and quality providers.

Provider plugins will depend on these services instead of reading files or launching subprocesses directly.

`ProcessManager` emits transport-neutral progress events. A future MCP tool will receive an MCP `Context` and adapt them to `ctx.report_progress`, while a CMake-specific parser can convert output such as `[ 42%]` into intermediate updates.
