# ForgeMCP

ForgeMCP is an MCP server that will provide AI assistants with deep, structured integration for C++ development.

## Current Core MVP

The initial server exposes a diagnostic MCP tool over the stdio transport:

- `server_status`

`FORGEMCP_WORKSPACE` must name an existing workspace directory. The Core validates it but does not inspect project files.

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

The next increments are a Workspace module, then CMake build/test, clangd, and Debug Adapter Protocol modules.

## Core structure

- `core/config.py` — typed runtime configuration and workspace-root validation.
- `core/services.py` — explicit dependency registry for future modules.
- `core/application.py` — application composition, lifecycle, and server status.
- `core/errors.py` — expected Core errors and safe MCP-facing responses.
- `core/logging.py` — structured, redacted stderr logging.

See [architecture.md](docs/architecture.md) for Core boundaries and extension points.
