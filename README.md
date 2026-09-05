# ForgeMCP

ForgeMCP is a Python MCP server for structured C and C++ development workflows.
The repository currently contains a deliberately small vertical slice that establishes
the conventions for future workspace, CMake, clangd, quality, and debugger modules.

## Current MCP surface

- Tool `workspace_overview` returns a typed, bounded summary of the configured
  workspace, reports progress, carries an embedded icon, and opens an MCP App widget.
- Resource template `forgemcp://workspace/files/{extension}` lists bounded,
  workspace-relative C/C++ file paths.
- Prompt `inspect_cpp_workspace` starts a focused, read-only project inspection.
- Completions suggest supported resource extensions and prompt focus values.

The tool remains useful in clients without MCP Apps support because the Python SDK
serializes its typed result into both text `content` and `structuredContent`.

## Requirements

- Python 3.11 or newer
- Node.js `^20.19.0` or `>=22.12.0` only when changing widgets

The server is built against MCP Python SDK `2.1.1`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
```

The generated widget HTML is committed and included in the Python package. Rebuild it
after changing files under `frontend/`:

```powershell
npm ci --prefix frontend
npm run build --prefix frontend
```

## Run

Run against the included C++ acceptance project:

```powershell
.\.venv\Scripts\forgemcp.exe --workspace examples/cpp-acceptance-project
```

For interactive development with MCP Inspector:

```powershell
$env:FORGEMCP_WORKSPACE = "examples/cpp-acceptance-project"
.\.venv\Scripts\mcp.exe dev src/forgemcp/server.py
```

All MCP traffic uses stdout. Operational logs must go to stderr.

## Verify

```powershell
npm run build --prefix frontend
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

See [docs/architecture.md](docs/architecture.md) for module boundaries and the
registration lifecycle. Repository rules for coding agents live in [AGENTS.md](AGENTS.md).
