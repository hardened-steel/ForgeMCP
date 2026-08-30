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
- Process Runtime with bounded output, process-tree cleanup, scoped execution
  policy, and a trusted-adapter path with required ownership, exact executable
  approval, and scrubbed environment.
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
- DAP Phase 0: strict trusted-adapter process ownership, exact executable
  approval, scrubbed adapter environment, and local `lldb-dap` qualification.
  Standalone LLVM `lldb-dap` 22.1.8 passes version/start/close and an opt-in
  test-only `initialize`/`disconnect` stdio gate on this Windows host.
- DAP Phase 1: production bounded DAP transport; launch-only debugger plugin;
  opaque stopped-data handles; source breakpoints, execution control,
  inspection/evaluate, cursor events, fake-adapter lifecycle tests, and real
  standalone LLVM 22.1.8 PE/COFF + DWARF service/MCP stdio gates.
- DAP Phase 1 security/integration audit: strict response-command correlation,
  bounded pre-normalization event/reverse queues and teardown, strict MCP
  unknown-field rejection, stop pre-emption during configuration, capability-
  gated `configurationDone`, terminal-event retention, and minimal
  side-effect-possible identifier evaluate policy. Fake, real LLDB-DAP, MCP
  stdio, CMake, clangd, and full-suite regression pass on the audited host.
- Quality Tools Phase 1: builtin QualityPlugin with lazy fixed executable
  qualification, `clang-format` replacement-XML checks and snapshot-CAS
  multi-file applies, bounded read-only `clang-tidy` check/diagnostic parsing,
  and bounded ASan/UBSan report parsing. No `-i`, auto-fix, arbitrary tool
  arguments, plugin loading, or sanitizer binary execution is exposed.
- Quality Tools Phase 1 security/integration audit: canonical exact executable
  approval excludes workspace/current-directory PATH spoofing and detects
  replacement; formatter stdin snapshots and UTF-8 byte-offset conversion pass
  real LLVM 22.1.8 UTF-8/BOM/CRLF/multi-file gates; adversarial XML, diagnostics,
  sanitizer, CAS/concurrency/lifecycle, and real MCP stdio gates pass. The
  compilation database and parent/project tool configuration remain explicitly
  trusted input, not a sandbox.
- Project Intelligence Phase 1: application-scoped provider registry and
  transport-neutral health/activity aggregation exposed only as
  `project__status {}`. Eight builtin providers report bounded cached state;
  collection has per-provider/aggregate deadlines, safe partial failures, and
  cancellation cleanup. It performs no process, source, protocol, qualification,
  analysis, or lifecycle action. Git and aggregated diagnostic messages remain
  outside this phase.
- Project Intelligence Phase 1 security/integration audit: strict provider
  revalidation and timestamp policy, single-flight concurrent calls, bounded
  timeout/shutdown cleanup when cancellation is suppressed, deterministic
  100,000-byte response truncation metadata, critical-provider health rules,
  failed-start provider rollback, and real MCP SDK stdio failure/timeout/race
  coverage.
- UX Stabilization Phase A: immutable source-aware configuration, backwards-
  compatible argparse CLI (`doctor`/`print-config`), optional safe CMake build
  directory defaults, centralized cached toolchain discovery, Windows VS/Build
  Tools discovery and filtered Developer-environment capture. See ADR 0012.
- UX Stabilization Phase B: transport-neutral request execution/progress
  contract, FastMCP token adapter, bounded ProcessRuntime observer, CMake/
  CTest phase-heartbeat UX with strict Ninja/CTest exact parsing, and lifecycle
  phase progress for clang-tidy, clangd, and debugger. The independent
  security/concurrency/protocol audit and opt-in real Windows gate passed on
  2026-08-22. See ADR 0013.
- UX Stabilization Phase B.1: builtin Workspace MCP tools, bounded
  application-local post-commit mutation batches, CMake stale marking, source-
  aware compilation-database policy/validation, and clangd document/database
  coherence. The workspace/CMake/clangd coherence audit and opt-in real
  Windows gate passed on 2026-08-22. See ADR 0014. That phase did not include
  an external watcher or an MCP discovery surface; Git arrived separately in Phase 1.
- UX Stabilization Phase C: bounded server instructions, Resources, Prompts,
  legacy prompt/resource-template Completion, application-scoped structured
  logging fan-out, a sanitized recent-log ring, and connection-scoped MCP
  Logging. The official SDK stdio gate covers initialization, discovery,
  reads, pagination, prompts, completion context, log filtering, cancellation,
  and clean shutdown. See ADR 0015. Tasks, Experimental features, resource
  subscriptions, and an external watcher remain absent; Git is now a separate
  read-only feature with its own ADR.
- ForgeMCP Stabilization Phase D1: cached path-free CMake kits derived from
  central discovery; application-local CAS selection; deterministic per-kit
  build-directory suggestions; bounded existing-tree inspection/adoption;
  safe configure/build diagnostics; kit resources, prompts, completions, CLI
  configuration, and operator doctor matrix. CMake Tools state/scripts remain
  intentionally unsupported; see ADR 0016.
- ForgeMCP Stabilization Phase D2.4: one-switch production-discovery live
  acceptance, host-local dynamic SDK evidence, external build-tree adoption,
  and Phase C discovery/progress/disclosure gates on a disposable C++ fixture.
- Git Intelligence Phase 1: builtin read-only Git plugin with qualified exact
  discovery, porcelain/protocol parsing, bounded patches/history/blame/local
  branches, cached ProjectStatus integration, Workspace mutation invalidation,
  and disposable real SDK fixture coverage. No Git write or network operation.

### In progress

- Real LLVM feature gates remain portable conditional tests on hosts without
  separately installed qualified tools.
- The host-qualified runner invokes MSVC for a ready MSVC kit and standalone
  LLVM/DWARF debugger gates only for a qualified chain; it adds no MSVC/PDB
  debugger compatibility claim.

## Delivery sequence and dependencies

| Stage | Depends on | Readiness criterion |
| --- | --- | --- |
| CMake vertical slice | Core, Models, Workspace, Process Runtime, Plugin System | Complete; its integration audit passes. |
| clangd | Audited Core, Models, Workspace, Process Runtime, Plugin System | A managed LSP session can be started, initialized, stopped, and mapped to transport-neutral diagnostics without escaping the workspace. |
| DAP debugger | ADR 0009, audited Process Runtime, Plugin System, workspace path policy, a runnable approved adapter | A managed debug-adapter session can launch within policy, prove strong tree ownership, terminate descendants, and expose bounded transport-neutral debug state. |
| Quality tools | CMake target/build metadata and Process Runtime | Complete: lazy fixed-tool discovery, CAS formatting, bounded read-only diagnostics, and sanitizer parsing without shell access or secrets. |
| Project Intelligence Phase 1 | Audited Core, Workspace, Process Runtime, plugins, and feature caches | Complete: bounded cached provider aggregation with explicit partial/freshness and no refresh side effects. |
| UX Stabilization Phase C | Accepted Phase A, Phase B, and Workspace/CMake/clangd coherence baselines | Complete: bounded MCP discovery surface and connection logging pass the SDK protocol and adversarial gates. |
| Stabilization Phase D1 | Phase B accepted, Workspace/CMake/clangd coherence accepted, Phase C implemented | CMake kit selection, safe compiler/generator propagation, and compatible existing-tree adoption work without cache deletion. |
| Stabilization Phase D2 | D1 baseline and permanent C++ fixture | Full real-MCP surface is recorded against disposable fixture copies; Phase C+D share the final audit. |
| Git intelligence Phase 1 | Audited ProcessRuntime, Workspace path policy, plugins, ProjectStatus and discovery | Complete when all six read-only tools have real SDK coverage, fixed process controls hold, and the Git fixture gate is capability-qualified. |

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

## Completed milestone: DAP phase 1

The DAP design is fixed in
[ADR 0009](adr/0009-dap-architecture-backend-and-debugger-trust-model.md).
Phase 0 supplied the strict adapter admission boundary. Phase 1 now adds a
production `forgemcp.dap` framing/protocol/client implementation, an
application-scoped `DebuggerService`, a constrained `LldbDapBackend`, and the
builtin debugger plugin. clangd remains fully independent of debugger
lifecycle.

Delivered scope:

- a bounded `forgemcp.dap` transport/client, separate from LSP, with partial
  framing, command-correlated concurrent out-of-order responses, bounded
  event/reverse queues, reverse-request denial, capability-gated cancel,
  EOF/malformed failure, and sequential writes;
- an application-scoped builtin debugger plugin and exactly one active launch
  session per application;
- policy-approved direct stdio `lldb-dap`, starting only a workspace-contained
  build-tree executable with validated argv/CWD. The debuggee environment map
  is disabled until an explicit allow-list is configured;
- source breakpoints, continue/pause/step, threads, stack/scopes/variables,
  and a paused hover-context single-identifier evaluate (which may still have
  native debugger side effects); and
- `debugger__status`, `debugger__list_adapters`, `debugger__launch`,
  `debugger__stop`, `debugger__set_breakpoints`, `debugger__continue`,
  `debugger__pause`, `debugger__step_over`, `debugger__step_in`,
  `debugger__step_out`, `debugger__threads`, `debugger__stack_trace`,
  `debugger__scopes`, `debugger__variables`, `debugger__evaluate`, and
  `debugger__events`.

The Phase-0 gate to start implementation has passed:

- standalone LLVM `C:\Program Files\LLVM\bin\lldb-dap.exe` version 22.1.8
  passed fixed `--version` (exit code 0), required Windows Job ownership,
  scrubbed-environment controlled start/close, and a test-only
  `initialize`/`disconnect` stdio probe. The qualifier confirms no object or
  debug-information format yet;
- Visual Studio 2022 and 18 x64 copies fail with loader status `0xC0000135`
  because `liblldb.dll` is absent from the inspected approved companion
  directories; their ARM64 copies cannot start on this x64 host. No DLLs were
  copied or modified;

The Phase-1 delivery gates have passed on this host:

- deterministic fake transport and fake-adapter tests cover fragmented frames,
  out-of-order/command-mismatch responses, bounded event/reverse floods,
  reverse-request denial, timeout/cancellation, malformed input/EOF, deferred
  launch configuration, stop during configuration, event cursors/terminal
  retention, handles, stale invalidation, and shutdown;
- the standalone LLVM `C:\Program Files\LLVM\bin\lldb-dap.exe` 22.1.8 gate
  builds a local `-O0 -g -gdwarf-4` PE/COFF executable, confirms `.debug_info`
  via `llvm-readobj`, then passes initialize/launch/breakpoint/stopped/threads/
  stack/scopes/variables/evaluate/step/continue/disconnect cleanup; and
- the real MCP stdio vertical slice passes tools/list, adapter listing, launch,
  breakpoint/stop, threads through variables, continue, events, stop, and
  transport shutdown.

Phase 2 is intentionally separate: modules, read-memory, disassembly,
set-variable, write-memory, restart, and attach.  It requires individual DAP
capability checks and policy grants.  `cppvsdbg`/`OpenDebugAD7` is a later,
operator-installed Windows PDB backend; CodeLLDB/GDB are later optional
backends.  `runInTerminal`, `startDebugging`, arbitrary commands/adapter args,
remote/dump modes, source/symbol auto-download, and arbitrary external source
access are out of scope.

Remaining clangd candidates are semantic tokens, inlay hints, code lenses,
refactor/rewrite actions beyond pure WorkspaceEdit, and richer completion
resolve; each needs a bounded contract and safety review.

## Current MVP limitations

- CMake execution is for trusted workspace code only; configure, build, and
  tests are not an OS sandbox.
- CMake preset inheritance, conditions, macro expansion, and arbitrary CMake
  or CTest arguments remain owned by CMake and are not reimplemented.
- clangd intentionally omits semantic tokens, inlay hints, code lenses,
  arbitrary LSP methods, arbitrary execute-command, resource operations
  (Create/Rename/DeleteFile), and arbitrary clangd argv passthrough.
- Workspace MCP tools deliberately support bounded read/snapshot, guarded
  unified-patch creation, and existing-file text edits only; delete/rename,
  binary files, arbitrary paths, an external watcher, and Git integration
  remain unavailable. Phase C resources and prompts are bounded discovery
  aids; they add no filesystem mutation operation.
  `project__status` reports cached safe component metadata only and deliberately
  has no aggregated diagnostic messages or refresh. DAP Phase 1 is launch-only LLDB-DAP
  source debugging for the passed standalone LLVM/DWARF gate; it is not an
  MSVC/PDB compatibility claim.
- CMake and CTest use the application-scoped exact toolchain selection: an
  explicit CLI/environment executable wins, otherwise discovery may select a
  trusted Visual Studio or safe PATH/standalone candidate. When VS discovery
  succeeds, only CMake/CTest receive its filtered Developer environment.
  Quality tools retain their stricter exact-executable policy and never receive
  that environment.

## Open architecture decisions

- Decide a wider supported `lldb-dap` version floor and Windows DWARF
  toolchain matrix. Decide separately whether an MSVC/PDB tier uses tested
  LLDB-DAP or optional Microsoft `OpenDebugAD7` under its extension licensing.
- Define the separate security and process-tree design for any future
  `runInTerminal` broker before enabling terminal-dependent debuggees.
- Decide which quality-tool result schema can be shared without making domain
  models depend on any individual tool format.
- Define Git status freshness, ignored-file treatment, repository nesting, and
  trust policy before any separate Git provider is added.
