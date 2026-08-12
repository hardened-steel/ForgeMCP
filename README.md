# ForgeMCP

ForgeMCP is an MCP server that will provide AI assistants with deep, structured integration for C++ development.

## Current CMake and clangd slices

The server exposes a Core diagnostic tool and the built-in CMake feature plugin:

- `server_status`
- `cmake__status`
- `cmake__list_presets`
- `cmake__configure`
- `cmake__list_targets`
- `cmake__build`
- `cmake__ctest_list_tests`
- `cmake__ctest_run`
- `clangd__status`
- `clangd__start`
- `clangd__stop`
- `clangd__diagnostics`
- `clangd__hover`
- `clangd__definition`
- `clangd__references`
- `clangd__document_symbols`
- `clangd__workspace_symbols`

`FORGEMCP_WORKSPACE` must name an existing workspace directory. The Core validates it but does not inspect project files.

Feature integrations use the public `forgemcp.plugins` contract. CMake and clangd are built-in plugins; clangd does not start a process until `clangd__start` receives an explicit workspace-contained directory with `compile_commands.json`. `FORGEMCP_CLANGD` optionally names an absolute clangd executable; otherwise the Process Runtime uses its policy-approved PATH discovery. External entry-point plugins are disabled by default; enabling them requires both `FORGEMCP_EXTERNAL_PLUGINS_ENABLED=true` and an explicit comma-separated `FORGEMCP_EXTERNAL_PLUGIN_ALLOWLIST`. See [architecture.md](docs/architecture.md), [ADR 0005](docs/adr/0005-feature-plugin-contract-and-external-trust.md), and [ADR 0007](docs/adr/0007-managed-lsp-lifecycle-document-synchronization-and-uri-policy.md) before allowing third-party code or extending clangd.

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

CMake 3.23 or later is supported. Configure, build, and test intentionally run
the selected project's CMake logic and test executables; run ForgeMCP only for
workspaces whose project code you trust. See [ADR 0006](docs/adr/0006-cmake-file-api-build-directory-and-trust-boundary.md).

## Core structure

- `core/config.py` — typed runtime configuration and workspace-root validation.
- `core/services.py` — explicit dependency registry for future modules.
- `core/application.py` — application composition, lifecycle, and server status.
- `plugins/` — versioned feature-plugin contract, lifecycle manager, tool registry, and opt-in entry-point discovery.
- `cmake/` — built-in CMake/Ctest feature plugin, CMake-owned models, and File API parsing.
- `lsp/` — reusable transport-neutral JSON-RPC/LSP framing and request client.
- `clangd/` — managed read-only clangd feature plugin, models, and document synchronization.
- `core/errors.py` — expected Core errors and safe MCP-facing responses.
- `core/logging.py` — structured, redacted stderr logging.

See [architecture.md](docs/architecture.md) for Core boundaries and extension points.
