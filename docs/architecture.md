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

Workspace I/O itself is isolated in the separately composed Workspace module. MCP Workspace tool adapters and debug adapters remain intentionally unimplemented.

## LSP transport and clangd feature

`forgemcp.lsp` is a transport-neutral JSON-RPC 2.0/LSP stream adapter. It has no MCP SDK, Core, Workspace, or Process Runtime imports. `LspClient` owns Content-Length framing, one reader task, a monotonically increasing request-ID table, out-of-order response delivery, bounded inbound messages, request timeout/cancellation with `$/cancelRequest`, and safe failure propagation on malformed messages or EOF. It answers only minimal server-to-client requests (`workspace/configuration`, progress creation, capability registration, and a denied `workspace/applyEdit`); it is not a general LSP proxy.

`forgemcp.clangd` is an application-scoped builtin feature plugin with capability `clangd`. Its `ClangdService` receives only the declared `workspace` and `process_runtime` services through `PluginContext`; it does not receive FastMCP, ForgeApplication, or a raw registry. Every clangd child is launched through Process Runtime. `FORGEMCP_CLANGD` may set an absolute executable path; the default runtime adds that one path to its normal policy allow-list. Otherwise the permitted bare `clangd` name is discovered through the composition-time PATH captured by Process Runtime. No MCP argument can supply executable flags, `--query-driver`, or a path outside the workspace.

`clangd__start` requires an explicit workspace-contained, non-symlink directory with `compile_commands.json`, then launches only `clangd --compile-commands-dir=<validated-relative-directory>`. It performs `initialize` followed by `initialized`. On close, it sends `didClose` for every opened document, then `shutdown` and `exit`, closes the LSP streams, and waits before asking ProcessHandle to terminate the tree. Closing is idempotent. An unexpected process exit or failed protocol stream places the service in `failed`; there is no automatic restart loop. clangd stderr is continuously drained with a fixed retention limit and discarded without raw logging.

Document text is read only through WorkspaceService. On first use ForgeMCP sends `didOpen`; when a new `FileSnapshot` SHA-256 is observed, it sends a full `didChange` with a monotonically increasing version. Only snapshot, URI, version, and normalized diagnostics are retained, never a permanent source-text cache. `publishDiagnostics` is associated with the active snapshot/version. `clangd__diagnostics` reports completeness, timeout, and staleness; an empty current publication is a successful empty result.

The public coordinate policy is Unicode code-point columns. LSP's negotiated `utf-8`, `utf-16`, or `utf-32` character offset is converted only at the LSP adapter boundary, rejecting positions that split an encoded character. Input document paths are workspace-relative and checked by WorkspaceService. Incoming file URIs are percent-decoded and revalidated through WorkspaceService; results outside the workspace are omitted and reported only through an omitted-result count. See [ADR 0007](adr/0007-managed-lsp-lifecycle-document-synchronization-and-uri-policy.md).

Phase 2 extends the same `LspClient` and `ClangdService`, rather than adding a second LSP transport. It exposes normalized completion and signature help; declaration/type-definition/implementation navigation; prepare/rename; code-action summaries and controlled application; document/range formatting; call/type hierarchy; and source/header switching. Completion snippets are proposals only and are never written automatically. Raw LSP payloads are not exposed.

All mutating clangd tools share one WorkspaceEdit engine. It accepts only LSP `changes` and `TextDocumentEdit` entries from `documentChanges`; CreateFile, RenameFile, DeleteFile, external URIs, stale LSP document versions, malformed ranges, and overlapping edits reject the whole operation. The engine converts negotiated LSP positions back to public code points, reads every target through WorkspaceService, captures expected snapshots, then invokes `WorkspaceService.apply_text_edits`. That capability validates every workspace/symlink boundary and commits its staged multi-file plan with rollback, so a conflict or commit failure never leaves a partial edit. It returns only `FileChange` metadata, never replacement text. After success, open affected documents receive full `didChange` synchronization and older diagnostics become stale.

Code actions and hierarchy items are represented by opaque random handles, not client-supplied LSP objects. Handles are application-session-bound, bounded to 100 cached entries per kind, expire after two minutes, and are cleared on stop, crash, or any document change. `clangd__apply_code_action` resolves an action only to obtain a pure WorkspaceEdit; command-only actions and `workspace/executeCommand` are unsupported. LSP RequestCancelled and ContentModified map to distinct safe domain errors. Semantic tokens, inlay hints, code lenses, arbitrary execute-command, and all DAP capabilities remain unsupported. See [ADR 0008](adr/0008-atomic-workspace-edits-and-opaque-clangd-handles.md).

## Process Runtime module

`forgemcp.processes` owns safe asyncio execution for CMake, CTest, and later clangd and DAP modules. It is transport-neutral and registers no MCP tools; `server.py` remains a thin stdio adapter. `ForgeApplication.create()` composes it under `application.services["process_runtime"]`.

Its public API intentionally has two paths:

- `await ProcessRuntime.run(argv, cwd=".", environment=None, inherit_environment=None, timeout_seconds=None) -> ProcessResult` runs a short command, captures bounded UTF-8 stdout and stderr independently, and returns a completed result. A timeout returns `timed_out=True` and `exit_code=None`; caller cancellation is re-raised after process cleanup.
- `await ProcessRuntime.start(argv, ...) -> ProcessHandle` starts a long-lived protocol process. `ProcessHandle.stdin`, `.stdout`, and `.stderr` expose asyncio streams directly for clangd or a DAP adapter. `await handle.wait()`, `await handle.terminate()`, `await handle.kill()`, and `await handle.aclose()` provide explicit lifecycle control. A handle never accumulates a `ProcessResult`.

Every command is a non-empty NUL-free argv sequence and is launched only with `asyncio.create_subprocess_exec(..., shell=False)`. There is deliberately no `run_shell` API. The runtime resolves a bare executable against the environment captured at composition time, then invokes its resolved path; a per-launch `PATH` override cannot redirect the executable. The immutable `ProcessPolicy` controls executable names/absolute paths, workspace-relative CWD allow-list, default and maximum short-command timeouts, output limit (up to the domain-model maximum), termination grace period, and environment inheritance/override keys. Environment inheritance is enabled by default; overrides are denied unless the policy names their keys (or an explicitly trusted adapter chooses unrestricted overrides). CWD is required to exist beneath the configured workspace and cannot be absolute, traverse `..`, or cross a symlink.

Process output and complete argv/environment values are never logged. Completion logs contain only exit/timeout state and each stream's character count plus truncation bit. The runtime retains all live `ProcessHandle` instances; callers should await `ProcessRuntime.aclose()` during asynchronous host shutdown. `ForgeApplication.aclose()` provides the corresponding application-level hook. The MCP stdio adapter already awaits it in FastMCP's lifespan. `ForgeApplication.stop()` can bridge to the asynchronous lifecycle only when no event loop is active; async hosts must await `ForgeApplication.aclose()`.

On POSIX each child starts a new session and process group. Graceful cleanup signals the group with `SIGTERM`, then escalates to `SIGKILL`. On Windows each child gets `CREATE_NEW_PROCESS_GROUP` and is assigned a private standard-library `ctypes` Job Object with `KILL_ON_JOB_CLOSE`; closing that job removes non-detached descendants even when the direct child has already exited. Graceful cleanup first sends `CTRL_BREAK_EVENT` with a direct-child terminate fallback; job closure is the forced tree kill. If Job assignment is refused by a host-owned job, forced cleanup falls back to the built-in `taskkill /PID <pid> /T /F` through asyncio with all helper streams discarded. This avoids a `psutil` runtime dependency. As on other process-management APIs, a tool that deliberately creates an independent process group/session or requests Job breakaway can escape its parent's tree boundary; adapters must not enable either.

## CMake feature module

`forgemcp.cmake` is a transport-neutral builtin feature plugin. `CMakeService` discovers `cmake` and `ctest`, parses versions, and supports CMake 3.23 or later. It lists safe summaries from `CMakePresets.json` and `CMakeUserPresets.json`, intentionally omitting `environment` and `cacheVariables`; CMake itself remains responsible for preset inheritance, conditions, and macro expansion.

Every configure request supplies a workspace-contained `source_dir` and an explicitly selected workspace-contained generated `binary_dir`. Configure writes the File API `codemodel-v2` query via `GeneratedWorkspaceDirectory` and invokes `cmake -S ... -B ...` through Process Runtime. When a preset is selected, CMake receives `--preset`, but ForgeMCP still passes the validated `-B` value so the preset cannot direct execution to an external build tree. No raw shell command or generic extra-argument field is exposed. Optional cache values are restricted to CMake-style identifier keys and NUL-free scalar values.

Targets come only from CMake File API codemodel v2 replies, never `--target help`. Missing, stale, malformed, unsupported-version, symlinked, or out-of-workspace replies return a CMake domain error. Reported source, artifact, and build paths are revalidated through Workspace before they are exposed as workspace-relative strings. Build preserves a non-zero CMake exit as a `ProcessResult`, with optional multi-config name and bounded `parallel_jobs`. CTest test discovery uses `ctest --show-only=json-v1`; execution supports all tests or a generated escaped exact-name selection and exposes no client-supplied regex or arbitrary CTest arguments. Timeout and output bounds are those of Process Runtime.

Running configure, build, or tests is not sandboxing: CMake project scripts, custom commands, generators, build tools, and test executables may execute project-controlled code. The configured workspace is therefore a trust boundary, not an untrusted-input boundary. See ADR 0006.

## Error and logging policy

Expected operational errors inherit from `ForgeMCPError` and are converted with `to_mcp_error_response`. The response includes only a stable code and an intentional public message.

Logs are JSON records written to stderr, so they do not corrupt the MCP stdio protocol. Context keys related to file contents, credentials, tokens, cookies, and secrets are redacted.
