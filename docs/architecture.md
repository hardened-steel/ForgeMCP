# ForgeMCP architecture

## Core

`forgemcp.core` is the composition root for the MCP server. It owns explicit configuration, workspace-root validation, the small service registry, application lifecycle, expected domain errors, and structured stderr logging.

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

Workspace and Process Runtime are not feature plugins. They remain foundational, always-composed Core services and are available to a feature plugin only when it declares their names in `PluginMetadata.requires_services`. The explicitly composed builtin plugins are CMake and clangd; clangd's plugin starts only its lightweight service at application startup, never a clangd process, so a missing executable does not prevent ForgeApplication from running.

A plugin subclasses `ForgePlugin` and supplies immutable `PluginMetadata`:

- `plugin_id` is a unique lower-case stable identifier and its namespace for all contributed tools.
- `api_version` must equal the stable `PLUGIN_API_VERSION` (`"1"`).
- `requires` names other feature plugins; `requires_services` names Core services; and `provides` declares globally unique capabilities.
- `async start(context)` receives a `PluginContext` with the immutable configuration, structured logger, a declaration-scoped service facade, and a plugin-scoped tool facade. It never receives `ForgeApplication` or the raw `ServiceRegistry`.
- `async stop()` releases resources. `PluginManager` invokes it in reverse startup order.

Before starting any plugin, `PluginManager` validates API versions, plugin IDs, capabilities, Core-service requirements, and the entire dependency graph. It starts a deterministic lexical topological order, rolls back successfully started plugins if a later startup fails, and makes `aclose()` idempotent. `PluginStatus` exposes each plugin's ID, source, capabilities, state, and safe exception class name for diagnostics. Application shutdown closes plugins before the Process Runtime, so adapters can release their protocol handles before the runtime terminates any remaining child processes.

Plugins may register a `ToolContribution` through `context.tools`. A contribution is a mapping-based Python handler plus a description and may name an optional Pydantic input model; it has no MCP, FastMCP, LSP, CMake, or DAP implementation type. `ToolRegistry` qualifies its local name as `<plugin_id>__<tool_name>`, rejects duplicates, and retains the immutable registration. After application startup, `server.py` wraps each contribution in a FastMCP handler and projects the optional input model into a flat MCP JSON schema. Thus external and builtin plugins cannot receive a FastMCP instance or register arbitrary transport objects.

The built-in CMake plugin owns the stable contributions `cmake__status`, `cmake__list_presets`, `cmake__configure`, `cmake__list_targets`, `cmake__build`, `cmake__ctest_list_tests`, and `cmake__ctest_run`. Its local CMake service receives only the declared `workspace` and `process_runtime` services, never an application object or a transport object.

External plugins use Python entry points in the `forgemcp.plugins` group. Discovery is disabled by default and is enabled only by both `ForgeConfig.external_plugins_enabled=True` (or `FORGEMCP_EXTERNAL_PLUGINS_ENABLED=true`) and a non-empty explicit `ForgeConfig.external_plugin_allowlist` (or comma-separated `FORGEMCP_EXTERNAL_PLUGIN_ALLOWLIST`). The allow-list contains entry-point names, which must exactly equal the loaded plugin's `plugin_id`. ForgeMCP does not enumerate entry-point metadata while discovery is disabled and calls `EntryPoint.load()` only for listed names; all other advertised packages remain unimported.

## Extension points

Feature integrations use `PluginContext` rather than `application.services`. The always-present Core service names they can explicitly require are:

- `config` — `ForgeConfig`
- `logger` — `StructuredLogger`
- `workspace` — `WorkspaceService`, the safe filesystem capability for the configured workspace
- `process_runtime` — `ProcessRuntime`, the safe asynchronous external-tool capability for the configured workspace
- `plugins` — `PluginManager`, when a future plugin has a valid reason to depend on manager-owned status or registry data

## Workspace module

`forgemcp.workspace` is a transport-neutral filesystem service for exactly `ForgeConfig.workspace_root`; it has no MCP tool adapter and does not import `mcp.server`. `ForgeApplication.create()` composes it as `application.services.get("workspace")`.

Its public `WorkspaceService` API is:

- `list_files(path=".", recursive=False) -> tuple[FileSnapshot, ...]`
- `read_text(path) -> tuple[str, FileSnapshot]`
- `get_snapshot(path) -> FileSnapshot`
- `apply_unified_patch(patch, expected_snapshots) -> PatchResult`
- `require_directory(path=".") -> str`
- `open_generated_directory(path, create=False) -> GeneratedWorkspaceDirectory`
- `validate_reported_path(path, relative_to=".") -> str`

All supplied paths are workspace-relative strings. Absolute, drive-qualified, and parent-traversal paths are rejected. A requested path containing a symlink is rejected; directory listing does not follow and omits symlinks. The default immutable `WorkspacePolicy` excludes `.git`, `.venv`, `build`, `build-*`, and `cmake-build-*` directories, and provides bounded UTF-8 reads and patch input. Callers can compose `WorkspaceService` with another policy when their generated-directory conventions differ.

Every patch target must carry a compare-and-swap expectation: preferably the `FileSnapshot` returned by `get_snapshot`, or its SHA-256 for an existing file; `None` represents an expected absent file for creation. A snapshot conflict or hunk mismatch returns `PatchResult(applied=False)` before source files are changed. Patches are text-only unified diffs, staged beside their targets and committed with rollback backups; patch input and file content never enter Workspace log context. File change events are intentionally not implemented yet.

`GeneratedWorkspaceDirectory` is an intentionally narrow capability for a caller-declared generated directory: it can write and read bounded UTF-8 files, list direct non-symlink files, and snapshot generated files without exposing a `Path`. It applies the same workspace and symlink checks even when the directory matches the ordinary Workspace ignore policy. CMake uses it for `.cmake/api/v1/query/codemodel-v2` and File API replies; it does not directly manipulate build-tree paths.

Workspace I/O itself is isolated in the separately composed Workspace module.
MCP Workspace tool adapters remain intentionally unimplemented. The debugger
uses only `validate_execution_path`, a separate validation-only capability for
workspace-contained generated execution paths; it grants neither file reads
nor writes.

## LSP transport and clangd feature

`forgemcp.lsp` is a transport-neutral JSON-RPC 2.0/LSP stream adapter. It has no MCP SDK, Core, Workspace, or Process Runtime imports. `LspClient` owns Content-Length framing, one reader task, a monotonically increasing request-ID table, out-of-order response delivery, bounded inbound messages, request timeout/cancellation with `$/cancelRequest`, and safe failure propagation on malformed messages or EOF. It answers only minimal server-to-client requests (`workspace/configuration`, progress creation, capability registration, and a denied `workspace/applyEdit`); it is not a general LSP proxy.

`forgemcp.clangd` is an application-scoped builtin feature plugin with capability `clangd`. Its `ClangdService` receives only the declared `workspace` and `process_runtime` services through `PluginContext`; it does not receive FastMCP, ForgeApplication, or a raw registry. Every clangd child is launched through Process Runtime. `FORGEMCP_CLANGD` may set an absolute executable path; the default runtime adds that one path to its normal policy allow-list. Otherwise the permitted bare `clangd` name is discovered through the composition-time PATH captured by Process Runtime. No MCP argument can supply executable flags, `--query-driver`, or a path outside the workspace.

`clangd__start` requires an explicit workspace-contained, non-symlink directory with `compile_commands.json`, then launches only `clangd --compile-commands-dir=<validated-relative-directory>`. It performs `initialize` followed by `initialized`. clangd is an untrusted, fallible protocol peer: all incoming messages are size-bounded, parsed into normalized models at the adapter boundary, and neither raw payloads, compiler arguments, source/replacement text, nor stderr are logged. On close, it sends `didClose` for every opened document, then `shutdown` and `exit`, closes the LSP streams, and waits before asking ProcessHandle to terminate the tree. Closing is idempotent. An unexpected process exit or failed protocol stream places the service in `failed`; there is no automatic restart loop. clangd stderr is continuously drained with a fixed discard limit.

Document text is read only through WorkspaceService. On first use ForgeMCP sends `didOpen`; when a new `FileSnapshot` SHA-256 is observed, it sends a full `didChange` with a monotonically increasing version. Only snapshot, URI, version, and normalized diagnostics are retained, never a permanent source-text cache. `publishDiagnostics` is associated with the active snapshot/version. `clangd__diagnostics` reports completeness, timeout, and staleness; an empty current publication is a successful empty result.

The public coordinate policy is Unicode code-point columns. LSP's negotiated `utf-8`, `utf-16`, or `utf-32` character offset is converted only at the LSP adapter boundary, rejecting positions that split an encoded character. Input document paths are workspace-relative and checked by WorkspaceService. Incoming file URIs are percent-decoded and revalidated through WorkspaceService; results outside the workspace are omitted and reported only through an omitted-result count. See [ADR 0007](adr/0007-managed-lsp-lifecycle-document-synchronization-and-uri-policy.md).

Phase 2 extends the same `LspClient` and `ClangdService`, rather than adding a second LSP transport. It exposes normalized completion and signature help; declaration/type-definition/implementation navigation; prepare/rename; code-action summaries and controlled application; document/range formatting; call/type hierarchy; and source/header switching. Completion snippets are proposals only and are never written automatically. Raw LSP payloads are not exposed.

All mutating clangd tools share one WorkspaceEdit engine. It accepts only LSP `changes` and `TextDocumentEdit` entries from `documentChanges`; CreateFile, RenameFile, DeleteFile, external URIs, stale LSP document versions, malformed ranges, and overlapping edits reject the whole operation. Null and empty edit lists are no-ops. A request is capped at 100 files, 1,000 text edits, and 1 MiB of UTF-8 replacement text. The engine converts negotiated LSP positions back to public code points, reads every target through WorkspaceService, captures expected snapshots, then invokes `WorkspaceService.apply_text_edits`. Mutations are serialized only through commit: delayed responses must still match the original anchor snapshot, so concurrent mutations of one snapshot yield at most one commit and the rest conflict. Read-only requests remain concurrent. A stop marks the session closing before acquiring the commit boundary, so a request that completes after shutdown starts cannot write files. After a successful commit, open affected documents receive full `didChange` synchronization, cached handles are invalidated, and older diagnostics become stale.

The multi-file guarantee is deliberately narrower than crash-atomic filesystem transactions. Detected validation and snapshot conflicts are all-or-nothing: no target is changed. The service stages replacement files, then attempts rollback if an I/O failure occurs during `os.replace`; rollback is best effort and may itself fail, notably for locked files on Windows. A crash, power loss, or an external writer racing between the final snapshot check and replacement can still leave a partial or externally modified result. The API reports a commit error rather than claiming absolute atomicity.

Code actions and hierarchy items are represented by opaque random handles, not client-supplied LSP objects. Handles are application-session-bound, backed by a 100-entry-per-kind cache with 64 KiB maximum payload per entry, expire after two minutes on a monotonic clock, and are cleared on stop, crash, or any document change. After expiry removal, capacity uses FIFO eviction; an evicted, cross-kind, arbitrary, or prior-session ID is a safe handle-expired error. `clangd__apply_code_action` resolves an action only to obtain a pure WorkspaceEdit and revalidates its document snapshot after resolve; command-only actions and `workspace/executeCommand` are unsupported. LSP RequestCancelled and ContentModified map to distinct safe domain errors. Semantic tokens, inlay hints, code lenses, arbitrary execute-command, and all DAP capabilities remain unsupported. See [ADR 0008](adr/0008-atomic-workspace-edits-and-opaque-clangd-handles.md).

## Process Runtime module

`forgemcp.processes` owns safe asyncio execution for CMake, CTest, and later clangd and DAP modules. It is transport-neutral and registers no MCP tools; `server.py` remains a thin stdio adapter. `ForgeApplication.create()` composes it under `application.services["process_runtime"]`.

Its public API has normal and trusted-adapter paths:

- `await ProcessRuntime.run(argv, cwd=".", environment=None, inherit_environment=None, timeout_seconds=None) -> ProcessResult` runs a short command, captures bounded UTF-8 stdout and stderr independently, and returns a completed result. A timeout returns `timed_out=True` and `exit_code=None`; caller cancellation is re-raised after process cleanup.
- `await ProcessRuntime.start(argv, ...) -> ProcessHandle` starts a long-lived protocol process. `ProcessHandle.stdin`, `.stdout`, and `.stderr` expose asyncio streams directly for clangd or a DAP adapter. `await handle.wait()`, `await handle.terminate()`, `await handle.kill()`, and `await handle.aclose()` provide explicit lifecycle control. A handle never accumulates a `ProcessResult`.
- `await ProcessRuntime.start_trusted_adapter(argv, approved_path_directories=...) -> ProcessHandle` and its bounded `run_trusted_adapter` counterpart require an exact approved absolute executable, a scrubbed environment, and OS tree containment before returning. `ProcessHandle.required_ownership`, `.ownership_established`, and `.environment_mode` expose only those safe lifecycle facts.

Every command is a non-empty NUL-free argv sequence and is launched only with `asyncio.create_subprocess_exec(..., shell=False)`. There is deliberately no `run_shell` API. The runtime resolves a bare executable against the environment captured at composition time, then invokes its resolved path; a per-launch `PATH` override cannot redirect the executable. An exact `ProcessPolicy.allowed_executable_paths` approval requires an existing regular executable, rejects symlink/reparse traversal, records canonical-path and file metadata, compares Windows paths case-insensitively, and detects a replaced file at launch. The immutable policy also controls executable names, workspace-relative CWD allow-list, default and maximum short-command timeouts, output limit (up to the domain-model maximum), termination grace period, and environment inheritance/override keys. Environment inheritance is enabled by default; overrides are denied unless the policy names their keys. CWD is required to exist beneath the configured workspace and cannot be absolute, traverse `..`, or cross a symlink.

Process output and complete argv/environment values are never logged. Completion logs contain only exit/timeout state and each stream's character count plus truncation bit. The runtime retains all live `ProcessHandle` instances; callers should await `ProcessRuntime.aclose()` during asynchronous host shutdown. `ForgeApplication.aclose()` provides the corresponding application-level hook. The MCP stdio adapter already awaits it in FastMCP's lifespan. `ForgeApplication.stop()` can bridge to the asynchronous lifecycle only when no event loop is active; async hosts must await `ForgeApplication.aclose()`.

On POSIX each child starts a new session and process group. Graceful cleanup signals the group with `SIGTERM`, then escalates after the policy grace period to `SIGKILL`. On Windows each child gets `CREATE_NEW_PROCESS_GROUP`; the runtime creates a private standard-library `ctypes` Job Object with `KILL_ON_JOB_CLOSE` and no breakaway flags before launching, then verifies assignment. Closing that job removes non-detached descendants even when the direct child has already exited. Normal callers retain a `taskkill /PID <pid> /T /F` fallback after observed Job-assignment failure. A trusted adapter does not: if the Job cannot be created or assigned, its direct process is immediately reaped, no handle is returned, and `ProcessOwnershipError` reports that required ownership was unavailable. Its scrubbed environment inherits no ForgeMCP variables; Windows receives only present `SystemRoot`, `WINDIR`, `ComSpec`, `TEMP`, `TMP`, and `PATHEXT`, plus a `PATH` built from the approved executable/companion directories. Normal CMake and clangd callers still inherit their composition-time environment. argv, environment, and raw process output never enter logs.

`LldbDapQualifier` is a transport-neutral, internal Phase-0 helper, not a DAP client or MCP tool. It reads a declarative `FORGEMCP_LLDB_DAP` path first, then local PATH/LLVM/Visual Studio/VS Code/local-toolchain candidates, and accepts an adapter only after fixed `--version`/`--help` probes and a start/close cycle succeed through `run_trusted_adapter`/`start_trusted_adapter`. Its `AdapterQualification` separates runnable-process facts from unverified DAP, object-format, and debug-information capabilities, and retains only safe probe exit statuses and a parsed version. An opt-in test-local `initialize`/`disconnect` gate can check a real installed adapter without introducing a second production DAP transport. Debuggee environment is intentionally not part of this adapter environment; a future DAP launch policy owns it.

## DAP debugger feature

`forgemcp.dap` is a production, transport-neutral DAP client, separate from
`forgemcp.lsp`. Its `framing`, `protocol`, and `client` modules own bounded
`Content-Length` parsing (8 KiB headers/1 MiB bodies), sequential outbound
frames, monotonic client sequences, concurrent out-of-order response routing,
events, timeout/cancellation, and safe EOF/malformed-message failure. The
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
breakpoints and `configurationDone`; the launch response is awaited afterwards,
which avoids LLDB-DAP's configuration-sequence deadlock. `stopped`,
`continued`, `exited`, `terminated`, `output`, and breakpoint events enter a
256-record/512-KiB normalized cursor ring. `debugger__events` returns only
bounded normalized events, `next_cursor`, eviction count, and truncation.
Final close clears the ring and all handles.

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
uses `context="hover"` and a conservative variable/member/index grammar;
backticks, semicolons, assignments, calls, REPL/watch contexts, and LLDB
command escape are unavailable. Attach, PDB claims, terminal brokering,
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

Targets come only from CMake File API codemodel v2 replies, never `--target help`. Missing, stale, malformed, unsupported-version, symlinked, or out-of-workspace replies return a CMake domain error. Reported source, artifact, and build paths are revalidated through Workspace before they are exposed as workspace-relative strings. Build preserves a non-zero CMake exit as a `ProcessResult`, with optional multi-config name and bounded `parallel_jobs`. CTest test discovery uses `ctest --show-only=json-v1`; execution supports all tests or a generated escaped exact-name selection and exposes no client-supplied regex or arbitrary CTest arguments. Timeout and output bounds are those of Process Runtime.

Running configure, build, or tests is not sandboxing: CMake project scripts, custom commands, generators, build tools, and test executables may execute project-controlled code. The configured workspace is therefore a trust boundary, not an untrusted-input boundary. See ADR 0006.

## Error and logging policy

Expected operational errors inherit from `ForgeMCPError` and are converted with `to_mcp_error_response`. The response includes only a stable code and an intentional public message.

Logs are JSON records written to stderr, so they do not corrupt the MCP stdio protocol. Context keys related to file contents, credentials, tokens, cookies, and secrets are redacted.
