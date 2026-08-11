# ADR 0001: Use explicit application composition and a small service registry

## Context

ForgeMCP will eventually integrate several stateful developer tools. Creating those dependencies from global imports or letting every module read environment variables would make tests fragile and make the server's active workspace ambiguous.

## Decision

Core creates one immutable `ForgeConfig` from environment variables at application creation time. `ForgeApplication` owns lifecycle and a small named `ServiceRegistry`. Future modules receive their dependencies through this application composition boundary. `server.py` remains an MCP transport adapter and does not own Core state.

## Consequences

Tests can create applications with explicit configuration and isolated workspace roots. Future module composition is clear, but service names are runtime-checked rather than fully type-checked. The Core intentionally does not yet provide plugin discovery or lifecycle hooks for third-party modules; those will be designed as a separate Plugin System module.
