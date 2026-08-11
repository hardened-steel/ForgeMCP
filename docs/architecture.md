# ForgeMCP architecture

## Core

`forgemcp.core` is the composition root for the MCP server. It owns explicit configuration, workspace-root validation, the small service registry, application lifecycle, expected domain errors, and structured stderr logging.

The Core does **not** read or edit project files, configure or build CMake projects, run processes, communicate with clangd, or debug binaries. Those responsibilities belong to future modules and must receive dependencies through `ServiceRegistry` rather than constructing global state.

`server.py` is a deliberately thin adapter: it creates `ForgeApplication`, starts it, binds Core's `server_status` diagnostic operation to MCP Python SDK's stdio server, and stops the application on exit.

## Extension points

A future module should expose a small service object and register it during application composition under a stable name. It obtains Core dependencies through `application.services`:

- `config` — `ForgeConfig`
- `logger` — `StructuredLogger`

Plugin discovery, dependency resolution, CMake, workspace I/O, clangd, debug adapters, and process execution are intentionally outside this initial Core boundary.

## Error and logging policy

Expected operational errors inherit from `ForgeMCPError` and are converted with `to_mcp_error_response`. The response includes only a stable code and an intentional public message.

Logs are JSON records written to stderr, so they do not corrupt the MCP stdio protocol. Context keys related to file contents, credentials, tokens, cookies, and secrets are redacted.
