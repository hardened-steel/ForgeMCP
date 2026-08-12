# ForgeMCP roadmap

This document is the single source of truth for project status, sequencing, and
readiness. Architecture belongs in [architecture.md](architecture.md); durable
irreversible choices belong in [adr/](adr/).

## Current status

### Done

- Core composition, configuration, MCP lifespan, error conversion, and safe
  stderr logging.
- Transport-neutral domain models.
- Workspace service and generated-directory capability.
- Process Runtime with bounded output, process-tree cleanup, and scoped
  execution policy.
- Versioned Plugin System with builtin composition and guarded external
  discovery.
- Builtin CMake/CTest vertical slice: status, preset summaries, configure,
  File API target inspection, build, and test execution.

### In progress

- Integration audit of the first vertical slice: real MCP stdio, lifecycle,
  safety boundaries, and a real-CMake smoke path.

## Delivery sequence and dependencies

| Stage | Depends on | Readiness criterion |
| --- | --- | --- |
| CMake vertical slice | Core, Models, Workspace, Process Runtime, Plugin System | Complete; its integration audit passes. |
| clangd | Audited Core, Models, Workspace, Process Runtime, Plugin System | A managed LSP session can be started, initialized, stopped, and mapped to transport-neutral diagnostics without escaping the workspace. |
| DAP debugger | Audited Process Runtime, Plugin System, and workspace path policy | A managed debug-adapter session can launch or attach within policy, terminate descendants, and expose transport-neutral debug state. |
| Quality tools | CMake target/build metadata and Process Runtime | Tool discovery, bounded execution, and diagnostics normalization work without exposing shell access or secrets. |
| Git and `project_status` | Workspace snapshots plus CMake/quality summaries | Read-only repository/project aggregation reports bounded, safe status with explicit freshness and failure states. |

## Next milestone: clangd

**Goal:** add a workspace-scoped clangd plugin, not a general language-server
proxy.

Scope:

- discover and validate an allowed clangd executable through Process Runtime;
- start one application-owned LSP session, perform initialize/shutdown, and
  guarantee cleanup through plugin lifecycle;
- translate document locations and diagnostics into the shared domain models;
- expose a minimal, documented MCP tool set for server status and diagnostic
  retrieval; and
- add unit, lifecycle, and real-stdio integration tests with portable skips
  when clangd is unavailable.

Ready when:

- all clangd processes are owned by Process Runtime and end on application
  shutdown, including handler failures;
- all client paths are workspace-relative, symlink-safe, and validated before
  LSP use;
- protocol messages, source text, compiler arguments, and environment values
  never enter logs;
- expected clangd and workspace errors have stable structured responses; and
- the full test suite passes, with real-tool tests skipped only for explicitly
  unavailable host prerequisites.

## Current MVP limitations

- CMake execution is for trusted workspace code only; configure, build, and
  tests are not an OS sandbox.
- CMake preset inheritance, conditions, macro expansion, and arbitrary CMake
  or CTest arguments remain owned by CMake and are not reimplemented.
- There are no Workspace MCP editing tools, clangd tools, DAP tools, quality
  tools, Git integration, or aggregate `project_status` tool yet.
- CMake and CTest are discovered through the Process Runtime environment; an
  installation outside `PATH` must be deliberately made available by the host
  policy/environment.

## Open architecture decisions

- Define the document synchronization and ownership model for clangd (open
  documents, disk snapshots, and concurrent user edits).
- Define DAP launch/attach policy, adapter discovery, and debug-binary trust
  boundary before exposing a debugger tool.
- Decide which quality-tool result schema can be shared without making domain
  models depend on any individual tool format.
- Define Git status freshness, ignored-file treatment, and repository nesting
  policy before adding `project_status`.
