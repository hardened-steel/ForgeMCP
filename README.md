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

## Verify

```powershell
.\.venv\Scripts\python.exe -m build --wheel
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

See [docs/architecture.md](docs/architecture.md) for module boundaries and the
registration lifecycle. Repository rules for coding agents live in [AGENTS.md](AGENTS.md).
