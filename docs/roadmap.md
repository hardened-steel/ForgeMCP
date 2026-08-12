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
- clangd phase 1 and phase 2: managed LSP lifecycle, snapshot-based document
  synchronization, diagnostics/navigation/symbols, safe snapshot-guarded
  semantic edits with best-effort I/O rollback,
  completion/signature help, code actions, formatting, and hierarchy handles
  through the builtin `clangd` feature plugin.
- Security and integration audit for clangd phases 1–2: real stdio/MCP and
  clangd gates, bounded WorkspaceEdit input, serialized mutation commits,
  lifecycle cancellation, opaque-handle eviction, and regression coverage.

### In progress

- DAP design only: define launch/attach policy, adapter discovery, and the
  debug-binary trust boundary before any debugger implementation.

## Delivery sequence and dependencies

| Stage | Depends on | Readiness criterion |
| --- | --- | --- |
| CMake vertical slice | Core, Models, Workspace, Process Runtime, Plugin System | Complete; its integration audit passes. |
| clangd | Audited Core, Models, Workspace, Process Runtime, Plugin System | A managed LSP session can be started, initialized, stopped, and mapped to transport-neutral diagnostics without escaping the workspace. |
| DAP debugger | Audited Process Runtime, Plugin System, and workspace path policy | A managed debug-adapter session can launch or attach within policy, terminate descendants, and expose transport-neutral debug state. |
| Quality tools | CMake target/build metadata and Process Runtime | Tool discovery, bounded execution, and diagnostics normalization work without exposing shell access or secrets. |
| Git and `project_status` | Workspace snapshots plus CMake/quality summaries | Read-only repository/project aggregation reports bounded, safe status with explicit freshness and failure states. |

## Completed milestone: clangd phase 1

**Goal:** add a workspace-scoped clangd plugin, not a general language-server
proxy.

Scope:

- discover and validate an allowed clangd executable through Process Runtime;
- start one application-owned LSP session, perform initialize/shutdown, and
  guarantee cleanup through plugin lifecycle;
- translate document locations and diagnostics into the shared domain models;
- expose the documented read-only status, diagnostic, navigation, and symbol
  MCP tools; and
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

Delivered tools are `clangd__status`, `clangd__start`, `clangd__stop`,
`clangd__diagnostics`, `clangd__hover`, `clangd__definition`,
`clangd__references`, `clangd__document_symbols`, and
`clangd__workspace_symbols`. See [ADR 0007](adr/0007-managed-lsp-lifecycle-document-synchronization-and-uri-policy.md) for the fixed lifecycle, coordinate, and URI policies.

## Completed milestone: clangd phase 2

Phase 2 extends the same managed `forgemcp.lsp` and `ClangdService` layer; it
does not introduce an arbitrary LSP proxy. `WorkspaceService.apply_text_edits`
is the only file-writing path for clangd semantic edits. Rename, resolved pure
code actions, and document/range formatting use its snapshot-guarded staged
commit. Detected conflicts are all-or-nothing; I/O failure triggers best-effort
rollback, not a crash-atomic filesystem transaction. Completion and signatures
remain read-only; snippets are never applied automatically. Code-action and
hierarchy server objects are held only behind bounded-lifetime opaque handles.

The VS LLVM x64 clangd gate passed with an explicit `FORGEMCP_CLANGD` path for
phase-1 start/diagnostics/hover/definition/references/stop and phase-2 rename.
The VS Code clangd extension path was visible to PowerShell but not to Python's
filesystem view in this host, so it was not used as an executable. The normal
PATH discovery was absent in this environment.

## Next milestone: DAP design decision

The completed clangd audit permits DAP **design**, not DAP implementation. No
debug adapter has been implemented, and clangd remains independent of any
debugger lifecycle. A DAP proposal needs its own launch/attach trust policy,
adapter discovery, cancellation/termination contract, and transport-neutral
models. Remaining clangd candidates are semantic tokens, inlay hints, code
lenses, refactor/rewrite actions beyond pure WorkspaceEdit, and richer
completion resolve; each needs a bounded contract and safety review.

## Current MVP limitations

- CMake execution is for trusted workspace code only; configure, build, and
  tests are not an OS sandbox.
- CMake preset inheritance, conditions, macro expansion, and arbitrary CMake
  or CTest arguments remain owned by CMake and are not reimplemented.
- clangd intentionally omits semantic tokens, inlay hints, code lenses,
  arbitrary LSP methods, arbitrary execute-command, resource operations
  (Create/Rename/DeleteFile), and arbitrary clangd argv passthrough.
- There are no Workspace MCP editing tools, DAP tools, quality tools, Git
  integration, or aggregate `project_status` tool yet.
- CMake and CTest are discovered through the Process Runtime environment; an
  installation outside `PATH` must be deliberately made available by the host
  policy/environment.

## Open architecture decisions

- Define DAP launch/attach policy, adapter discovery, and debug-binary trust
  boundary before exposing a debugger tool.
- Decide which quality-tool result schema can be shared without making domain
  models depend on any individual tool format.
- Define Git status freshness, ignored-file treatment, and repository nesting
  policy before adding `project_status`.
