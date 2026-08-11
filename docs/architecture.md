# ForgeMCP architecture

## Core

`forgemcp.core` is the composition root for the MCP server. It owns explicit configuration, workspace-root validation, the small service registry, application lifecycle, expected domain errors, and structured stderr logging.

The Core does **not** read or edit project files, configure or build CMake projects, run processes, communicate with clangd, or debug binaries. Those responsibilities belong to future modules and must receive dependencies through `ServiceRegistry` rather than constructing global state.

`server.py` is a deliberately thin adapter: it creates `ForgeApplication`, starts it, binds Core's `server_status` diagnostic operation to MCP Python SDK's stdio server, and stops the application on exit.

## Domain models

`forgemcp.models` is an independent, transport-neutral contract package for future Workspace, process-runtime, CMake, clangd, and debugger services. It depends only on Pydantic and the Python standard library; in particular, it does not import Core, MCP, CMake, LSP, DAP, or process-library types.

The public API is exported from `forgemcp.models`:

- `Position`, `Range`, and `Location` describe source coordinates. Lines and columns are zero-based; ranges are half-open and cannot run backwards. A location's URI is opaque to this package, so each adapter owns its path-to-URI mapping.
- `Severity` and `Diagnostic` describe bounded, user-facing findings without adopting any producer's wire format.
- `TaskState` and terminal-only `TaskResult` describe the outcome of background work.
- `ProcessOutput` and `ProcessResult` keep stdout and stderr separate. Each captured stream is capped at 65,536 Unicode code points and signals loss with `truncated`.
- `FileSnapshot`, `FileChangeKind`, `FileChange`, and `PatchResult` report file state and atomic patch effects using metadata only. These models deliberately have no source-content or patch-text field, so they may be used as structured log context. Process output is not log-safe; call `ProcessOutput.log_summary()` when logging it.

Every model is immutable, rejects unknown fields, and has Pydantic field descriptions for JSON-schema consumers. All timestamps are timezone-aware `datetime` values normalized to UTC. Naive timestamps are invalid; `model_dump(mode="json")` and `model_dump_json()` render the UTC values as ISO-8601 strings. Models are value objects, not services, and must not inspect the workspace or execute processes.

## Extension points

A future module should expose a small service object and register it during application composition under a stable name. It obtains Core dependencies through `application.services`:

- `config` — `ForgeConfig`
- `logger` — `StructuredLogger`

Plugin discovery, dependency resolution, CMake, workspace I/O, clangd, debug adapters, and process execution are intentionally outside this initial Core boundary.

## Error and logging policy

Expected operational errors inherit from `ForgeMCPError` and are converted with `to_mcp_error_response`. The response includes only a stable code and an intentional public message.

Logs are JSON records written to stderr, so they do not corrupt the MCP stdio protocol. Context keys related to file contents, credentials, tokens, cookies, and secrets are redacted.
