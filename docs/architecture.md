# ForgeMCP architecture

## Core

`forgemcp.core` is the composition root for the MCP server. It owns explicit configuration, workspace-root validation, the small service registry, application lifecycle, expected domain errors, and structured stderr logging.

### Configuration and toolchain discovery (Phase A)

`ForgeConfig` is immutable and is composed only in Core from CLI, then
`FORGEMCP_*` environment, then defaults. It stores safe provenance categories
but no public raw environment values. `forgemcp` remains a stdio server with no
subcommand; stdlib-argparse `doctor` and `print-config` are local sanitized
commands. `ToolchainDiscoveryService` is another application-scoped Core
service. It supplies exact approved executables to CMake, clangd, Quality and
Debugger; feature modules do not independently inspect environment/PATH.

It caches discovery at startup, so `project__status` observes cached state only.
On Windows it uses trusted standard-location `vswhere.exe`, deterministic VS
instance/component/architecture selection, and a fixed-script filtered
Developer environment capture. Only exact selected CMake/CTest commands receive
that bounded build environment; clangd, Quality, and debugger retain their
stricter executable/environment policies. Its public diagnostics contain availability,
source category and rejection category, never host paths or raw environment.
See [ADR 0012](adr/0012-configuration-cli-and-windows-toolchain-discovery.md).

The Core does **not** implement project-file reads or edits, configure or build CMake projects, run processes, communicate with clangd, or debug binaries. It composes the Workspace service but leaves Workspace filesystem policy and business logic in `forgemcp.workspace`. Other modules must receive dependencies through `ServiceRegistry` rather than constructing global state.

`server.py` is a deliberately thin adapter: FastMCP's async lifespan creates and starts `ForgeApplication`, exposes it as the lifespan context for Core's `server_status` diagnostic operation, adapts already-registered tool contributions to the MCP SDK, and always awaits `application.aclose()` in `finally`. This covers normal transport shutdown and failures without creating a nested event loop.

## Domain models

`forgemcp.models` is an independent, transport-neutral contract package for Workspace, process-runtime, and future shared service models. It depends only on Pydantic and the Python standard library; in particular, it does not import Core, MCP, CMake, LSP, DAP, or process-library types. CMake- and clangd-specific result and metadata models deliberately live in their feature packages, rather than making the shared package depend on one feature.

The public API is exported from `forgemcp.models`:

- `Position`, `Range`, and `Location` describe source coordinates. Lines and columns are zero-based Unicode code points; ranges are half-open and cannot run backwards. A location's URI is opaque to this package, so each adapter owns its path-to-URI mapping and any protocol-specific encoding conversion.
- `Severity` and `Diagnostic` describe bounded, user-facing findings without adopting any producer's wire format.
- `TaskState` and terminal-only `TaskResult` describe the outcome of background work.
- `ProcessOutput` and `ProcessResult` keep stdout and stderr separate. Each captured stream is capped at 65,536 Unicode code points and signals loss with `truncated`. Output is opaque text, so leading and trailing whitespace (including line terminators) is preserved.
- `FileSnapshot`, `FileChangeKind`, `FileChange`, and `PatchResult` report file state and atomic patch effects using metadata only. These models deliberately have no source-content or patch-text field, so they may be used as structured log context. Process output is not log-safe; call `ProcessOutput.log_summary()` when logging it.

Every model is immutable, rejects unknown fields, and has Pydantic field descriptions for JSON-schema consumers. All timestamps are timezone-aware `datetime` values normalized to UTC. Naive timestamps are invalid; `model_dump(mode="json")` and `model_dump_json()` render the UTC values as ISO-8601 strings. Models are value objects, not services, and must not inspect the workspace or execute processes.

## Feature plugins

`forgemcp.plugins` is the public, transport-neutral extension contract for optional CMake, clangd, debugger, and future integrations. `ForgeApplication.create()` always composes a `PluginManager` as `application.services["plugins"]`; it registers ForgeMCP's `CMakePlugin` explicitly during composition and accepts `builtin_plugins=` for additional owned feature plugins. CMake's state belongs to that plugin instance and therefore to one application instance.

Workspace and Process Runtime are foundational Core services. Workspace's MCP adapter is the builtin `WorkspacePlugin`; filesystem policy and mutation logic remain in `forgemcp.workspace`. The explicitly composed builtin plugins are Workspace, CMake, clangd, debugger, Project, and Quality. clangd and Quality start only lightweight services at application startup, never a tool process, so a missing executable does not prevent ForgeApplication from running.

A plugin subclasses `ForgePlugin` and supplies immutable `PluginMetadata`:

- `plugin_id` is a unique lower-case stable identifier and its namespace for all contributed tools.
- `api_version` must equal the stable `PLUGIN_API_VERSION` (`"1"`).
- `requires` names other feature plugins; `requires_services` names Core services; and `provides` declares globally unique capabilities.
- `async start(context)` receives a `PluginContext` with the immutable configuration, structured logger, a declaration-scoped service facade, and a plugin-scoped tool facade. It never receives `ForgeApplication` or the raw `ServiceRegistry`.
- `async stop()` releases resources. `PluginManager` invokes it in reverse startup order.

Before starting any plugin, `PluginManager` validates API versions, plugin IDs, capabilities, Core-service requirements, and the entire dependency graph. It starts a deterministic lexical topological order, rolls back successfully started plugins if a later startup fails, and makes `aclose()` idempotent. `PluginStatus` exposes each plugin's ID, source, capabilities, state, and safe exception class name for diagnostics. Application shutdown closes plugins before the Process Runtime, so adapters can release their protocol handles before the runtime terminates any remaining child processes.

Plugins may register a `ToolContribution` through `context.tools`. A contribution is a mapping-based Python handler plus a description and may name an optional Pydantic input model; it has no MCP, FastMCP, LSP, CMake, or DAP implementation type. `ToolRegistry` normally qualifies its local name as `<plugin_id>__<tool_name>` and rejects duplicates. A contribution may use another validated stable namespace only when its plugin metadata declares it; Quality uses `quality`, `clang_format`, `clang_tidy`, and `sanitizer` under one lifecycle. A legacy handler remains `handler(arguments)`. A handler opts into context only with the exact keyword-only `execution_context: ToolExecutionContext`; positional/default `context` parameters remain legacy input and are never rebound. Contexts are constructed by `server.py` for one invocation, never stored by services, reused, serialized, or placed in schemas, and contain no SDK request/session/transport objects. `ProgressUpdate` is immutable, bounded and path/control-text-safe; its private request state keeps phase, heartbeat, exact parser and terminal numbers monotonic. `NoOpProgressReporter` keeps in-process and token-less clients behaviourally identical. After application startup, `server.py` wraps each contribution in a FastMCP handler and projects the optional input model, including Pydantic required fields and bounds, into a flat MCP JSON schema. Thus external and builtin plugins cannot receive a FastMCP instance or register arbitrary transport objects.

The built-in CMake plugin owns the stable contributions `cmake__status`, `cmake__list_presets`, `cmake__configure`, `cmake__list_targets`, `cmake__build`, `cmake__ctest_list_tests`, and `cmake__ctest_run`. Its local CMake service receives only the declared `workspace` and `process_runtime` services, never an application object or a transport object.

External plugins use Python entry points in the `forgemcp.plugins` group. Discovery is disabled by default and is enabled only by both `ForgeConfig.external_plugins_enabled=True` (or `FORGEMCP_EXTERNAL_PLUGINS_ENABLED=true`) and a non-empty explicit `ForgeConfig.external_plugin_allowlist` (or comma-separated `FORGEMCP_EXTERNAL_PLUGIN_ALLOWLIST`). The allow-list contains entry-point names, which must exactly equal the loaded plugin's `plugin_id`. ForgeMCP does not enumerate entry-point metadata while discovery is disabled and calls `EntryPoint.load()` only for listed names; all other advertised packages remain unimported.

## Extension points

Feature integrations use `PluginContext` rather than `application.services`. The always-present Core service names they can explicitly require are:

- `config` — `ForgeConfig`
- `logger` — `StructuredLogger`
- `workspace` — `WorkspaceService`, the safe filesystem capability for the configured workspace
- `process_runtime` — `ProcessRuntime`, the safe asynchronous external-tool capability for the configured workspace
- `toolchain_discovery` — cached `ToolchainDiscoveryService` exact tool choices and its private filtered build environment; plugins may use executable selections but must not serialize host paths/environment
- `plugins` — `PluginManager`, when a future plugin has a valid reason to depend on manager-owned status or registry data
- `project_status_registry` — `ProjectStatusRegistry`, optionally declared by a
  feature plugin that can expose a bounded cached `ComponentStatus`
- `project_status_service` — `ProjectStatusService`, consumed only by the
  builtin ProjectPlugin that contributes `project__status`

## Project Intelligence Phase 1

`forgemcp.project` is transport-neutral and application-scoped. One
`ForgeApplication` remains the one-workspace session boundary; there is no
`ProjectSession`. `ProjectStatusRegistry` owns uniquely identified providers and
collects them concurrently in deterministic order with bounded per-provider and
aggregate deadlines. Overlapping requests share exactly one in-flight snapshot;
one client's cancellation does not cancel it, and a call after completion starts
a fresh snapshot. Timeout and shutdown cancel providers and attempt a 50 ms
bounded join. A provider that suppresses cancellation remains tracked until
completion and its eventual exception is consumed. Cooperative async providers
therefore cannot extend the response indefinitely; CPU-blocking or malicious
in-process code cannot be safely pre-empted by asyncio. A provider failure
produces a safe partial result and cannot fail the complete `project__status`
response.

The builtin provider IDs are `core`, `workspace`, `process_runtime`,
`plugin_manager`, `cmake`, `clangd`, `debugger`, and `quality`. Feature plugins
register adapters through the declaration-scoped `project_status_registry`
service and unregister on stop; the aggregator has no imports of concrete
feature services. This uses the existing PluginContext/service mechanism and
does not change plugin API version 1. External plugins may optionally declare
and use the registry; not providing status does not affect plugin startup.

Every provider copies only cached state. Status performs no filesystem/source
read, process/version probe, build/test/configure, format/tidy, clangd/debugger
start or request, lifecycle mutation, polling, or refresh. Strict immutable
models allow bounded scalar facts only and omit argv/environment, output,
diagnostic messages, source/patch content, PIDs, debugger data, executable and
external-plugin paths, and raw exceptions. Build and compilation-database paths
are workspace-relative; only the configured root is absolute.

External providers are trusted in-process extensions, not sandboxed code, but
their result is still revalidated at the MCP boundary. The registry accepts only
`ComponentStatus`, revalidates its serialized fields, requires the registered
and returned IDs to match, rejects observations over five seconds in the future
or over 24 hours old, and forces age-based staleness after five minutes.
Construction bypasses, unknown fields, invalid enums/scalars, duplicate facts or
capabilities, naive timestamps, and oversize fields become fixed failed-provider
categories without exception text.

Health and activity are independent. A failed, missing, or invalid foundational
component makes health failed; optional provider loss/timeouts, failed optional
sessions/plugins, and observed unavailable explicitly configured capabilities
make it degraded. Optional
unconfigured tools and unsuccessful project operations do not mean ForgeMCP is
unhealthy. A paused debugger wins activity, active CMake/Quality/debugger work
or clangd startup is busy, and all other cases are idle. Component timestamps
make the bounded result explicitly partial/non-transactional. Capacity is 64
providers/components, 128 aggregate capabilities, 32 facts and 32 warnings per
component, and 32 aggregate warnings. The complete response is capped at
100,000 UTF-8 bytes; overflow omits components in reverse lexical order and
reports `response_truncated` and sorted `omitted_components`. See
[ADR 0011](adr/0011-project-status-provider-and-health-model.md).

clangd status counts at most 64 cached document records (each already bounded
to 1,000 diagnostics) while holding its document-state lock. If more documents
are open, the exact open-document count remains available but diagnostic counts
are marked `diagnostic_counts_truncated` and the component is stale; status does
not scan an unbounded cache or acquire the WorkspaceEdit mutation lock.

## Workspace module

`forgemcp.workspace` is a transport-neutral filesystem service for exactly `ForgeConfig.workspace_root`; it has no MCP dependency. `WorkspacePlugin` contributes `workspace__list_files`, `workspace__read_text`, `workspace__get_snapshot`, `workspace__apply_unified_patch`, and `workspace__apply_text_edits` through the normal ToolContribution contract. It returns relative paths and content-free snapshot metadata; mutation texts never enter logs/errors. Delete and rename are deliberately absent from this public tool surface.

Its public `WorkspaceService` API is:

- `list_files(path=".", recursive=False) -> tuple[FileSnapshot, ...]`
- `read_text(path) -> tuple[str, FileSnapshot]`
- `get_snapshot(path) -> FileSnapshot`
- `apply_unified_patch(patch, expected_snapshots) -> PatchResult`
- `require_directory(path=".") -> str`
- `open_generated_directory(path, create=False) -> GeneratedWorkspaceDirectory`
- `validate_reported_path(path, relative_to=".") -> str`

All supplied paths are workspace-relative strings. Absolute, drive-relative,
UNC/device, alternate-data-stream, reserved-device, trailing-dot/space, and
parent-traversal spellings are rejected. A requested path containing a symlink,
junction, or other reparse point is rejected; directory listing omits them and
is capped at 1,000 regular files. The default immutable `WorkspacePolicy`
excludes `.git`, `.venv`, `build`, `build-*`, and `cmake-build-*` directories,
and provides bounded UTF-8 reads and patch input. Callers can compose
`WorkspaceService` with another policy when their generated-directory
conventions differ.

After a successful staged commit and staging cleanup, Workspace emits exactly
one deterministically path-ordered, application-local mutation batch. It
contains only an application-local monotonic generation, operation ID,
relative path, change kind, and prior/current snapshot metadata. Publication
and every subscriber run after the Workspace filesystem lock is released. Each
subscriber has one bounded worker/queue; failure, timeout, cancellation
suppression, or saturation is sticky degraded integration state and cannot undo
the filesystem commit. The bounded history lets configure detect a relevant
batch that arrived after its generation capture; a history gap is conservatively
stale. The bus is owned by one ForgeApplication and is not an external
filesystem watcher.

Every patch target must carry a compare-and-swap expectation: preferably the
`FileSnapshot` returned by `get_snapshot`, or its SHA-256 for an existing file;
`None` represents an expected absent file for creation. A snapshot conflict or
hunk mismatch returns `PatchResult(applied=False)` before source files are
changed. Patches are text-only unified diffs, staged beside their targets and
committed with rollback backups; patch input and file content never enter
Workspace log context, errors, status, progress, or mutation events. A
validated no-op returns success without replacement or a mutation batch. The
public listing and edit collections are bounded (1,000 files/edits), as are
patch/replacement input and aggregate staged UTF-8 output before the first
write.

`GeneratedWorkspaceDirectory` is an intentionally narrow capability for a caller-declared generated directory: it can write and read bounded UTF-8 files, list direct non-symlink files, and snapshot generated files without exposing a `Path`. It applies the same workspace and symlink checks even when the directory matches the ordinary Workspace ignore policy. CMake uses it for `.cmake/api/v1/query/codemodel-v2` and File API replies; it does not directly manipulate build-tree paths.

Workspace I/O itself is isolated in the separately composed Workspace module.
The builtin Workspace adapter exposes only bounded list/read/snapshot,
unified-patch creation/modification, and existing-file text edits; its strict
input and success/error result schemas forbid unknown fields. The debugger uses only
`validate_execution_path`, a separate validation-only capability for
workspace-contained generated execution paths; it grants neither file reads
nor writes.

## LSP transport and clangd feature

`forgemcp.lsp` is a transport-neutral JSON-RPC 2.0/LSP stream adapter. It has no MCP SDK, Core, Workspace, or Process Runtime imports. `LspClient` owns Content-Length framing, one reader task, a monotonically increasing request-ID table, out-of-order response delivery, bounded inbound messages, request timeout/cancellation with `$/cancelRequest`, and safe failure propagation on malformed messages or EOF. It answers only minimal server-to-client requests (`workspace/configuration`, progress creation, capability registration, and a denied `workspace/applyEdit`); it is not a general LSP proxy.

`forgemcp.clangd` is an application-scoped builtin feature plugin with capability `clangd`. Its `ClangdService` receives only the declared `workspace`, `process_runtime`, and cached `toolchain_discovery` services through `PluginContext`; it does not receive FastMCP, ForgeApplication, or a raw registry. Every clangd child is launched through Process Runtime. `FORGEMCP_CLANGD` may set an absolute executable path; otherwise the central discovery service chooses one exact policy-approved candidate. No MCP argument can supply executable flags, `--query-driver`, or a path outside the workspace.

`clangd__start` may receive an explicit workspace-contained, non-symlink compilation-database directory, otherwise it uses the latest CMake-validated profile. `off` permits fallback command inference. It launches only fixed clangd arguments and performs `initialize` followed by `initialized`. A validated database fingerprint change triggers one bounded controlled restart/reinitialize and reopens only previously tracked documents; a restart failure degrades clangd but does not revise the CMake configure result. clangd is an untrusted, fallible protocol peer: all incoming messages are size-bounded, parsed into normalized models at the adapter boundary, and neither raw payloads, compiler arguments, source/replacement text, nor stderr are logged. On close, it sends `didClose` for every opened document, then `shutdown` and `exit`, closes the LSP streams, and waits before asking ProcessHandle to terminate the tree. Closing is idempotent. An unexpected process exit or failed protocol stream places the service in `failed`; there is no automatic restart loop. clangd stderr is continuously drained with a fixed discard limit.

Document text is read only through WorkspaceService. On first use ForgeMCP sends
`didOpen`; for each committed changed snapshot it sends at most one full
`didChange` with a strictly increasing version. Workspace mutations (including
clangd's own WorkspaceEdit) invalidate cached actions/hierarchy/diagnostics,
resynchronize only already tracked documents, and keep untracked paths
dirty/lazy. A notification failure leaves the older synchronized snapshot in
place, marks synchronization pending/degraded, and is retried from the next
safe document request; it never claims the stale snapshot was synchronized.
Only snapshot, URI, version, and normalized diagnostics are retained, never a
permanent source-text cache. `publishDiagnostics` is associated with the active
snapshot/version. `clangd__diagnostics` reports completeness, timeout, and
staleness; an empty current publication is a successful empty result.

The public coordinate policy is Unicode code-point columns. LSP's negotiated `utf-8`, `utf-16`, or `utf-32` character offset is converted only at the LSP adapter boundary, rejecting positions that split an encoded character. Input document paths are workspace-relative and checked by WorkspaceService. Incoming file URIs are percent-decoded and revalidated through WorkspaceService; results outside the workspace are omitted and reported only through an omitted-result count. See [ADR 0007](adr/0007-managed-lsp-lifecycle-document-synchronization-and-uri-policy.md).

Phase 2 extends the same `LspClient` and `ClangdService`, rather than adding a second LSP transport. It exposes normalized completion and signature help; declaration/type-definition/implementation navigation; prepare/rename; code-action summaries and controlled application; document/range formatting; call/type hierarchy; and source/header switching. Completion snippets are proposals only and are never written automatically. Raw LSP payloads are not exposed.

All mutating clangd tools share one WorkspaceEdit engine. It accepts only LSP `changes` and `TextDocumentEdit` entries from `documentChanges`; CreateFile, RenameFile, DeleteFile, external URIs, stale LSP document versions, malformed ranges, and overlapping edits reject the whole operation. Null and empty edit lists are no-ops. A request is capped at 100 files, 1,000 text edits, and 1 MiB of UTF-8 replacement text. The engine converts negotiated LSP positions back to public code points, reads every target through WorkspaceService, captures expected snapshots, then invokes `WorkspaceService.apply_text_edits`. Mutations are serialized only through commit: delayed responses must still match the original anchor snapshot, so concurrent mutations of one snapshot yield at most one commit and the rest conflict. Read-only requests remain concurrent. A stop marks the session closing before acquiring the commit boundary, so a request that completes after shutdown starts cannot write files. After a successful commit, open affected documents receive full `didChange` synchronization, cached handles are invalidated, and older diagnostics become stale.

The multi-file guarantee is deliberately narrower than crash-atomic filesystem transactions. Detected validation and snapshot conflicts are all-or-nothing: no target is changed. The service stages replacement files, then attempts rollback if an I/O failure occurs during `os.replace`; rollback is best effort and may itself fail, notably for locked files on Windows. A crash, power loss, or an external writer racing between the final snapshot check and replacement can still leave a partial or externally modified result. The API reports a commit error rather than claiming absolute atomicity.

Code actions and hierarchy items are represented by opaque random handles, not client-supplied LSP objects. Handles are application-session-bound, backed by a 100-entry-per-kind cache with 64 KiB maximum payload per entry, expire after two minutes on a monotonic clock, and are cleared on stop, crash, or any document change. After expiry removal, capacity uses FIFO eviction; an evicted, cross-kind, arbitrary, or prior-session ID is a safe handle-expired error. `clangd__apply_code_action` resolves an action only to obtain a pure WorkspaceEdit and revalidates its document snapshot after resolve; command-only actions and `workspace/executeCommand` are unsupported. LSP RequestCancelled and ContentModified map to distinct safe domain errors. Semantic tokens, inlay hints, code lenses, arbitrary execute-command, and all DAP capabilities remain unsupported. See [ADR 0008](adr/0008-atomic-workspace-edits-and-opaque-clangd-handles.md).

## Process Runtime module

`forgemcp.processes` owns safe asyncio execution for CMake, CTest, and later clangd and DAP modules. It is transport-neutral and registers no MCP tools; `server.py` remains a thin stdio adapter. `ForgeApplication.create()` composes it under `application.services["process_runtime"]`.

Short-command `run` optionally accepts a trusted local `ProcessOutputObserver`. It receives independently incrementally decoded 4,096-character-bounded stdout/stderr chunks through one 32-event bounded queue and worker; stream ordering is not promised. Pipe drain and `ProcessResult` capture remain independent of observer speed. Overflow drops observations and marks safe `ProcessResult.observer_overflow`; observer exceptions, a slow observer, or cancellation suppression are isolated and mark `observer_failed` without delaying the process result. A local observer may provide an optional bounded `aclose()` flush for an EOF-terminated partial line. Raw chunks are never logged, retained after dispatch, or forwarded automatically to MCP. This is solely a local parser hook for fixed progress derivation; protocol `start` streams and stdin ownership remain unchanged.

Its public API has normal and trusted-adapter paths:

- `await ProcessRuntime.run(argv, cwd=".", environment=None, inherit_environment=None, timeout_seconds=None, input_data=None) -> ProcessResult` runs a short command, optionally feeds at most 1 MiB of opaque stdin bytes, closes stdin, captures bounded UTF-8 stdout and stderr independently, and returns a completed result. Stdin bytes are never logged. A timeout returns `timed_out=True` and `exit_code=None`; caller cancellation is re-raised after process cleanup.
- `await ProcessRuntime.start(argv, ...) -> ProcessHandle` starts a long-lived protocol process. `ProcessHandle.stdin`, `.stdout`, and `.stderr` expose asyncio streams directly for clangd or a DAP adapter. `await handle.wait()`, `await handle.terminate()`, `await handle.kill()`, and `await handle.aclose()` provide explicit lifecycle control. A handle never accumulates a `ProcessResult`.
- `await ProcessRuntime.run_toolchain(argv, cwd=".", timeout_seconds=None) -> ProcessResult` is internal build integration: it admits only exact discovery-pinned CMake/CTest executables and has no caller environment parameter. When a filtered VS environment exists, only this path receives it.
- `await ProcessRuntime.start_trusted_adapter(argv, approved_path_directories=...) -> ProcessHandle` and its bounded `run_trusted_adapter` counterpart require an exact approved absolute executable, a scrubbed environment, and OS tree containment before returning. `ProcessHandle.required_ownership`, `.ownership_established`, and `.environment_mode` expose only those safe lifecycle facts.

Every command is a non-empty NUL-free argv sequence and is launched only with `asyncio.create_subprocess_exec(..., shell=False)`. There is deliberately no `run_shell` API. The runtime resolves a bare executable against the environment captured at composition time, then invokes its resolved path; a per-launch `PATH` override cannot redirect the executable. An exact `ProcessPolicy.allowed_executable_paths` approval requires an existing regular executable, rejects symlink/reparse traversal, records canonical-path and file metadata, compares Windows paths case-insensitively, and detects a replaced file at launch. The immutable policy also controls executable names, workspace-relative CWD allow-list, default and maximum short-command timeouts, output limit (up to the domain-model maximum), termination grace period, and environment inheritance/override keys. Environment inheritance is enabled by default; overrides are denied unless the policy names their keys. CWD is required to exist beneath the configured workspace and cannot be absolute, traverse `..`, or cross a symlink.

Process output and complete argv/environment values are never logged. Completion logs contain only exit/timeout state and each stream's character count plus truncation bit. The runtime retains all live `ProcessHandle` instances; callers should await `ProcessRuntime.aclose()` during asynchronous host shutdown. `ForgeApplication.aclose()` provides the corresponding application-level hook. The MCP stdio adapter already awaits it in FastMCP's lifespan. `ForgeApplication.stop()` can bridge to the asynchronous lifecycle only when no event loop is active; async hosts must await `ForgeApplication.aclose()`.

On POSIX each child starts a new session and process group. Graceful cleanup signals the group with `SIGTERM`, then escalates after the policy grace period to `SIGKILL`. On Windows each child gets `CREATE_NEW_PROCESS_GROUP`; the runtime creates a private standard-library `ctypes` Job Object with `KILL_ON_JOB_CLOSE` and no breakaway flags before launching, then verifies assignment. Closing that job removes non-detached descendants even when the direct child has already exited. Normal callers retain a `taskkill /PID <pid> /T /F` fallback after observed Job-assignment failure. A trusted adapter does not: if the Job cannot be created or assigned, its direct process is immediately reaped, no handle is returned, and `ProcessOwnershipError` reports that required ownership was unavailable. Its scrubbed environment inherits no ForgeMCP variables; Windows receives only present `SystemRoot`, `WINDIR`, `ComSpec`, `TEMP`, `TMP`, and `PATHEXT`, plus a `PATH` built from the approved executable/companion directories. ForgeMCP removes all `FORGEMCP_*` variables from ordinary child inheritance. When VS discovery succeeds, CMake/CTest alone receive the separately filtered Developer environment; clangd, Quality, and debugger do not. argv, environment, and raw process output never enter logs. This contains the owned normal process tree, including adapter descendants on adapter crash; it cannot absolutely cover OS/power crashes or a trusted compromised process deliberately escaping the platform containment primitive.

`LldbDapQualifier` is a transport-neutral, internal Phase-0 helper, not a DAP client or MCP tool. Production backend executable selection comes from the central discovery service; qualifier tests retain fixed `--version`/`--help` probes and a start/close cycle through `run_trusted_adapter`/`start_trusted_adapter`. Its `AdapterQualification` separates runnable-process facts from unverified DAP, object-format, and debug-information capabilities, and retains only safe probe exit statuses and a parsed version. An opt-in test-local `initialize`/`disconnect` gate can check a real installed adapter without introducing a second production DAP transport. Debuggee environment is intentionally not part of this adapter environment; a future DAP launch policy owns it.

## DAP debugger feature

`forgemcp.dap` is a production, transport-neutral DAP client, separate from
`forgemcp.lsp`. Its `framing`, `protocol`, and `client` modules own bounded
`Content-Length` parsing (8 KiB headers/1 MiB bodies), sequential outbound
frames, monotonic client sequences, command-correlated concurrent out-of-order
response routing, events, timeout/cancellation, and safe EOF/malformed-message
failure. Pre-normalization event and reverse-request queues are bounded to 16
and 8 records respectively (with four fixed reverse workers); saturation,
malformed input, a wrong response command, or an unexpected EOF fails pending
requests with a bounded error, closes adapter stdin, and tears down workers.
The
client never owns a process or imports Core, Workspace, MCP, or LLDB code.
It denies every reverse request: `runInTerminal` and `startDebugging` have
explicit policy failures and all other reverse requests are unsupported.

`forgemcp.debugger` owns the one-session state machine, workspace launch
validation, opaque-handle lifetime, normalized debug models, event buffering,
and builtin `DebuggerPlugin`. It receives only declared Workspace and Process
Runtime services; it never receives FastMCP or a raw registry. Every adapter
starts only through `ProcessRuntime.start_trusted_adapter` after exact
executable approval, scrubbed environment construction, and required process
tree ownership. The service continuously drains adapter stderr into a bounded
discard counter; no raw DAP, stdout, stderr, argv, environment value, source
contents, variable value, evaluation expression, or evaluation result is
logged.

The primary Phase 1 backend is a separately installed, policy-approved LLVM
`lldb-dap` executable over stdio.  The default Windows source-debugging target
is a compatible PE/COFF build with DWARF; a particular MSVC/PDB combination is
not promised until it passes its own real-adapter integration gate.  The
Microsoft `cppvsdbg`/`OpenDebugAD7` adapter is a later optional Windows PDB
backend that must be discovered from a compatible installed C/C++ extension
and never redistributed.  GDB DAP and CodeLLDB are deferred; WinDbg has no
verified standalone DAP adapter in this design.

Phase 1 implements `UNAVAILABLE → STOPPED → STARTING → INITIALIZED →
CONFIGURING → RUNNING/PAUSED → TERMINATING → TERMINATED`, with `FAILED` as a
safe terminal failure path. `initialized` is awaited before initial source
breakpoints and `configurationDone` when the adapter explicitly advertises
`supportsConfigurationDoneRequest`; the launch response is awaited afterwards,
which avoids LLDB-DAP's configuration-sequence deadlock. `debugger__stop`
pre-empts a pending `STARTING`/`CONFIGURING` launch by cancellation, then uses
the same bounded disconnect/tree-close path; a new launch is accepted only
after the previous resource cleanup reaches `TERMINATED`. `stopped`,
`continued`, `exited`, `terminated`, `output`, and breakpoint events enter a
256-record/512-KiB normalized cursor ring. `debugger__events` returns only
bounded normalized events, `next_cursor`, eviction count, and truncation.
`debugger__stop` retains one normalized terminal event for post-stop reading;
the ring is cleared before the next session and on full application shutdown.
All handles are cleared on close.

Only workspace-relative existing program, CWD, and source breakpoint paths are
accepted. `WorkspaceService.validate_execution_path` is a validation-only
capability that safely includes ignored generated build trees while refusing
links/reparse traversal; executable replacement after validation is the
documented residual race. A launch has at most 64 separate NUL-free arguments,
always uses LLDB `console="internalConsole"`, and currently accepts only an
empty debuggee environment map because no environment allow-list is configured.
Source breakpoint sets are full replacements and accept only zero-based
line/column positions. Adapter-reported external sources are returned only as
omitted metadata; their contents are never read.

Native DAP IDs never leave the service. Random bounded-TTL opaque handles are
typed (`thread`, `frame`, `scope`, `variables`, `breakpoint`), bound to the
application session and, for paused data, stop generation. Continue/step,
pause/continued events, stop, crash, or session exit invalidate stopped-data
handles and cancel pending paused reads. Read-only inspection runs only while
paused and confirms its captured stop generation before returning. Evaluate
uses `context="hover"` and accepts only one ASCII identifier lookup; member
access, indexing, dereference, casts, overloaded operators, calls, assignment,
comments, whitespace/confusables, REPL/watch contexts, and LLDB command escape
are unavailable. This deliberately small grammar reduces egress and mutation
surface but does **not** make native evaluate side-effect-free: LLDB may still
execute debuggee evaluation semantics. Variables/scopes are the primary
read-only inspection path. Attach, PDB claims, terminal brokering,
memory/disassembly/mutation, restart, conditional/function/data breakpoints,
source/symbol download, and arbitrary adapter/LLDB commands remain unsupported.

The builtin tool surface is `debugger__status`, `debugger__list_adapters`,
`debugger__launch`, `debugger__stop`, `debugger__set_breakpoints`,
`debugger__continue`, `debugger__pause`, `debugger__step_over`,
`debugger__step_in`, `debugger__step_out`, `debugger__threads`,
`debugger__stack_trace`, `debugger__scopes`, `debugger__variables`,
`debugger__evaluate`, and `debugger__events`. All source coordinates exposed
by ForgeMCP are zero-based; DAP's one-based coordinates are translated only at
the LLDB/backend boundary.

## CMake feature module

`forgemcp.cmake` is a transport-neutral builtin feature plugin. `CMakeService` discovers `cmake` and `ctest`, parses versions, and supports CMake 3.23 or later. It lists safe summaries from `CMakePresets.json` and `CMakeUserPresets.json`, intentionally omitting `environment` and `cacheVariables`; CMake itself remains responsible for preset inheritance, conditions, and macro expansion.

Every configure request supplies a workspace-contained `source_dir` and an explicitly selected workspace-contained generated `binary_dir`. Configure writes the File API `codemodel-v2` query via `GeneratedWorkspaceDirectory` and invokes `cmake -S ... -B ...` through Process Runtime. When a preset is selected, CMake receives `--preset`, but ForgeMCP still passes the validated `-B` value so the preset cannot direct execution to an external build tree. No raw shell command or generic extra-argument field is exposed. Optional cache values are restricted to CMake-style identifier keys and NUL-free scalar values.

The immutable compile-commands policy is CLI, then environment, then default
`auto`; the allowed modes are `auto`, `required`, and `off`. `auto` adds
`CMAKE_EXPORT_COMPILE_COMMANDS=ON`; qualified Ninja is selected only with no
explicit generator/preset or cached generator, in an empty generated tree, and
with a compatible selected toolchain environment. An explicit preset is
preserved even when its inherited generator is not locally expanded. Existing
CMake cache generator changes are rejected with an empty-build-directory
suggestion. Only exact Ninja/Ninja Multi-Config and named Makefile generator
families are database-capable; Visual Studio is not claimed to produce one.
After configure ForgeMCP reads the actual cache generator, then validates a
byte-bounded regular UTF-8 JSON database inside the generated build tree before
parsing and exposes availability/support/count/fingerprint metadata only.
Database commands and external paths are trusted project input for native
tools, not sandboxed input, and are never returned or logged. Configure captures
the Workspace generation before execution; a relevant later mutation keeps the
successful result stale. Workspace CMake-file mutations only mark cached
configuration stale; they do not configure automatically.

Targets come only from CMake File API codemodel v2 replies, never `--target help`. Missing, stale, malformed, unsupported-version, symlinked, or out-of-workspace replies return a CMake domain error. Reported source, artifact, and build paths are revalidated through Workspace before they are exposed as workspace-relative strings. Build preserves a non-zero CMake exit as a `ProcessResult`, with optional multi-config name and bounded `parallel_jobs`. CTest test discovery uses `ctest --show-only=json-v1`; execution supports all tests or a generated escaped exact-name selection and exposes no client-supplied regex or arbitrary CTest arguments. Timeout and output bounds are those of Process Runtime.

Long CMake operations consume only their invocation's execution context. Configure/build/test use fixed phase labels and a two-second bounded heartbeat. Exact values are emitted solely for strict Ninja `[completed/total]` and strict CTest completion formats; a reset, changed total, oversized line, unrecognized/MSBuild/localized output remains heartbeat-only. Local parsers never copy process lines or project-controlled CTest names into progress. Terminal failure/cancellation does not claim completion; exact `total/total` is deferred until success. Before terminal configure success ForgeMCP validates its bounded File API model, compilation database, and post-config workspace generation; unavailable/invalid File API and stale generation become fixed warning semantics for a successful process result. `ProcessResult` additionally exposes derived duration and safe observer-health metadata.

Running configure, build, or tests is not sandboxing: CMake project scripts, custom commands, generators, build tools, and test executables may execute project-controlled code. The configured workspace is therefore a trust boundary, not an untrusted-input boundary. See ADR 0006.

## Error and logging policy

Expected operational errors inherit from `ForgeMCPError` and are converted with `to_mcp_error_response`. The response includes only a stable code and an intentional public message.

Logs are JSON records written to stderr, so they do not corrupt the MCP stdio protocol. Context keys related to file contents, credentials, tokens, cookies, and secrets are redacted.

## Quality feature

`forgemcp.quality` is a transport-neutral builtin feature module containing
`ClangFormatService`, `ClangTidyService`, `SanitizerReportParser`, immutable
Quality models, and `QualityPlugin`. It receives only Workspace, Process
Runtime, and cached Toolchain Discovery through `PluginContext`; it neither imports FastMCP nor receives
ForgeApplication. Its tools are `quality__status`, `clang_format__check`,
`clang_format__apply`, `clang_tidy__list_checks`, `clang_tidy__run`, and
`sanitizer__parse_report`.

Quality executable selection is fixed by the central discovery service, with an
absolute explicit CLI/environment choice
considered first, followed by Developer environment, selected VS, safe PATH,
and a small conventional installed LLVM location. Empty/relative PATH entries,
Windows current-directory search, and candidates inside the workspace are
excluded. Discovery records a
canonical regular non-link path and file metadata; qualification uses bounded
fixed `--version` and tool-specific `--help` probes, and every later launch uses
that exact approved path with replacement detection. Availability is reported
in status instead of failing startup. Executable paths, argv values, environment
values, source text, replacement data, and raw tool output are never logged. No
MCP argument can select an executable.

Formatter checks use `clang-format --output-replacements-xml` rather than
stdout full-file output or `-i`. The exact Workspace snapshot is supplied as
bounded stdin and a validated workspace-relative `--assume-filename` controls
language/config discovery, so a formatter never rereads a raced source path.
The XML rejects DTD/entities and unexpected structure, and only complete,
bounded, ordered, non-overlapping in-file ranges aligned to UTF-8 boundaries are
accepted. Clang tooling replacement offsets and lengths are bytes; they are
converted to Workspace Unicode code-point positions before commit. LF, CRLF,
mixed line endings, non-BMP code points, combining characters, EOF edits, missing
final newline, empty files, and a UTF-8 BOM are covered; BOM is preserved.
Apply first formats every requested file, requires and revalidates a SHA-256 for
every source snapshot (including no-op files), rejects a process/parse failure
before calling Workspace, then sends one non-overlapping `apply_text_edits`
batch. Detected snapshot conflict therefore changes no file. Ordinary commit I/O
failure triggers Workspace best-effort rollback; locks, rollback failure,
external final-replacement races, crash, and power loss remain outside any
filesystem-atomic guarantee.

With its default `style=file` behavior, clang-format may search parent
directories above the workspace and may follow a project-supplied symlinked
`.clang-format`/`_clang-format`; `InheritParentConfig` can extend that search.
Those format configurations are explicitly trusted operator/project input. They
are never returned to MCP or logged. ForgeMCP does not claim a sandbox boundary
for format configuration discovery.

clang-tidy accepts explicit source paths and one validated generated workspace
directory containing a regular non-link `compile_commands.json`. ForgeMCP
supplies only fixed `-p=<directory>`, optional one-element bounded
`--checks=<pattern>`, and option-safe relative source arguments. It never
publishes fixes, plugin loading, response files, the compiler-argument `--`
delimiter, extra compiler arguments, arbitrary config/header filters, or a
generic runner. Phase 1 parses compiler-style diagnostic output strictly instead
of adding a YAML dependency or applying export-fixes replacements. Drive-colon,
space/parenthesis, and relative paths are handled; ANSI/control syntax and
source/caret excerpts are discarded, and absolute paths embedded in semantic
messages are redacted. Clang diagnostic columns are one-based
UTF-8 byte columns and are boundary-checked and converted to code points.
External locations are counted and omitted; malformed/unmappable records are
counted separately. Capture/parser loss makes `complete=false`; `execution_state`
separates findings from timeout/tool failure. Stream order is stdout followed by
stderr because Process Runtime intentionally captures them separately.

The workspace project, parent/project `.clang-tidy` configuration, and its
CMake-generated compilation database are trusted inputs, not a sandbox boundary.
Database commands may contain frontend/plugin flags and external include paths,
and clang-tidy may read external headers; ForgeMCP adds none of those flags,
never returns an external diagnostic path/content or a raw command, and makes no
safety claim for analysis of an untrusted project.

The sanitizer parser consumes bounded supplied text only. It recognizes
AddressSanitizer, UndefinedBehaviorSanitizer, and an unknown fallback; strips
terminal controls; emits fixed normalized summaries/categories rather than raw
report lines; returns at most 32 findings and 64 bounded workspace-only frames
per finding with opaque addresses; omits path-like external frames; and marks
partial or truncated parsing. The unknown fallback never copies its input. It
performs no process launch, symbolizer/network/source access, source/symbol
download, or instrumented-binary execution.
