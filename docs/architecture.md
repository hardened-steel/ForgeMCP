# ForgeMCP architecture

## Status

ForgeMCP is intentionally at foundation stage. The `workspace` feature manages
project files and a separate service-storage root. The shared `process` service adds asynchronous
external-program lifecycle, text transcripts, timeouts, and a read-only inspection
surface for development commands. CMake now configures, builds, and tests operator
profiles. Read-only clangd analysis is mounted through MCP tools and workspace
result extensions; its widgets remain deferred. Quality and debugger remain future work.
The `toolchain` service discovers independent toolsets once at startup and exposes
their paths and on-demand versions through a read-only MCP surface.

The design goal is a small composition root plus independent feature services. There
is no plugin system, service locator, event bus, repository layer, or transport-neutral
adapter hierarchy. Add one only when concrete behavior makes it necessary.

## Source layout

```text
src/forgemcp/
  server.py                     # composition root, CLI, MCPServer
  assets.py                     # package-relative Widget and IconFile helpers
  completion.py                 # one server-wide completion dispatcher
  assets/*.html                 # generated single-file widgets
  icons/*                       # source icon files packaged with the server
  <feature>/
    service.py                  # business logic, service class, MCP handlers
    errors.py                   # expected feature errors
  process/
    service.py                  # subprocess lifecycle, streams, state, MCP inspection
    models.py                   # session results and MCP-facing process state
    errors.py                   # expected process failures
  toolchain/
    service.py                  # toolset container, Python API, MCP registration
    discovery.py                # provider orchestration and ready spec assembly
    loader.py                   # deterministic pkgutil built-in enumeration
    spec.py                     # tool metadata, ready specs, and toolset containers
    errors.py                   # expected configuration and command errors
    providers/                  # system PATH, explicit CLI, Visual Studio layouts
    tools/                      # INFO, create_spec, and typed methods per tool
tests/
  conftest.py                   # isolated copy of the C++ acceptance workspace
  <feature>/test_service.py     # direct business behavior
  test_server.py                # MCP surface and protocol behavior
frontend/
  src/shared/                   # common TUI styles, renderer, formatting, Apps bridge
  src/*.js                      # small feature widget entrypoints
  tests/                        # Node + jsdom behavior tests; no browser required
  *.html                        # Vite entry points
examples/cpp-acceptance-project # portable CMake fixture
```

Create extra feature files only around a concrete responsibility. For example, a
CMake module may grow `output_parser.py` and `kits.py`; it should not start with
interfaces and factories for hypothetical parsers or kit providers.

## Composition and dependency injection

`src/forgemcp/server.py` is the only composition root:

1. Resolve operator configuration such as the workspace root.
2. Construct `WorkspaceService`, then `ProcessService` with the project and storage
   directories as allowed working roots, then feature services with explicit dependencies.
3. Create one `Apps` instance.
4. Create one `Complete` completion collector.
5. Construct `MCPServer(extensions=[apps])`, composing its instructions from the base
   text and every service class docstring.
6. Call each service's single `register(mcp, apps, complete)` method.
7. Mount the validated Apps tools and resources through their public bindings and
   register the one server-wide completion handler.
8. Run the selected transport; stdio is the default and the workspace defaults to the
   server process working directory. Before accepting requests, the server lifespan
   awaits toolchain discovery; it closes remaining managed processes during shutdown,
   including when discovery fails.

Steps 3-7 are deliberately visible. MCP Python SDK 2.1 fixes extensions at server
construction and consumes their bindings at that point. The empty `Apps` extension is
therefore supplied to the constructor for capability negotiation, then the bindings
collected by service registration are mounted through the SDK's public APIs. This
keeps one feature registration method without a generic plugin framework or private
SDK access.

A service may depend on another service, but receives it in `__init__`. It never
creates that dependency itself. This keeps tests local and makes application state
ownership visible in `server.py`.

## Feature service contract

A typical feature has these methods:

```python
class ExampleService:
    """Describe this feature for the model-facing server instructions."""

    WIDGET = Widget("assets/example.html")
    ICON = IconFile("icons/example.svg")

    def __init__(self, dependency: DependencyService) -> None: ...

    def business_operation(self, ...) -> Result: ...

    def register(self, mcp: MCPServer, apps: Apps, complete: Complete) -> None:
        @apps.tool(resource_uri=self.WIDGET.uri, icons=[self.ICON.icon])
        async def example_tool(value: str, ctx: Context) -> Result:
            """Describe the tool; the SDK infers its public metadata and schema."""
            ...
```

Only methods that the feature actually needs are present. The nested MCP entrypoints
close over the long-lived service instance, so intentional state survives calls while
the class remains focused on reusable business operations. The SDK derives names,
descriptions, schemas, and structured output from the entrypoint signatures and
docstrings. A local completion function returns `None` for references the feature does
not own and is added to the shared `Complete` collector.

## Current MCP surface

### Workspace files

`WorkspaceService` owns path checks, directory trees, metadata, UTF-8 reading,
literal/regex searching, mutations, and storage directories. The ten tools are
`workspace_list`, `workspace_find_files`, `workspace_file_info`, `workspace_read_file`,
`workspace_search`, `workspace_write_file`, `workspace_edit_file`, `workspace_move`,
`workspace_delete`, and `workspace_mkdir`. The former overview, file-extension
resource, and inspection prompt are removed. There are no workspace prompts.

Tree and search operations live in async handlers inside `register`, reused by
Markdown resources. They yield during traversal and report visited entries/files
without an invented total. Workspace, process, and toolchain use the same progress helper. Each tool
invocation has its own progress throttle,
using a monotonic clock and the shared `forgemcp.progress.progress(ctx, interval=1.0)` helper. The CLI
option is `--progress-interval`; zero disables throttling. `create_server` validates the interval once before constructing services, then
injects it into all services. Services and reporters use the validated setting directly. All progress
notifications, including start/completion, obey the minimum interval. The first
notification is immediate, skipped updates are not queued, and counters still
advance for every work unit. No timer, delayed send, or operation wrapper is used.
The tool response signals completion. File reading reports bytes as chunks are read. Small
reusable filesystem operations remain service methods; their tool handlers report
completion of one operation. No generic runner or thread offloading is used.
Individual filesystem calls are synchronous; traversal yields cooperatively between
work units. Results remain small Pydantic models. The SDK derives structured output and useful JSON text fallback.
Expected `WorkspaceError` failures become `ToolError` or `ResourceError` at the
corresponding boundary. OS errors retain the system-provided `strerror`, which may be localized. Resource roots are explicitly checked before filesystem access,
including mirrors without a path, to avoid opaque SDK validation errors. Platform-specific file ownership is isolated in
`workspace/metadata.py`; unavailable owner or creation time is null.

Project/storage paths shared between modules and in tool arguments/results use
`WorkspacePath`, serialized as a string such as `project/src/main.cpp` or
`storage/build/debug`. The roots themselves are `project/` and `storage/`.
Executable locations and low-level filesystem APIs still use native `Path` values.
There is no separate `root` tool argument. Path traversal,
absolute paths, and resolutions outside the selected root are rejected. Trees
display symlinks/junctions without descending into them. Trees hide dot-prefixed
directories by default (`include_hidden=true` includes them); dot files stay visible. Explicit reads may follow
in-root links; mutations reject linked path components. Moving or recursively
removing a directory containing links is rejected. Workspace roots cannot be
mutated. `protect_path(WorkspacePath(...))` lets dependent modules protect files or
directories, including paths that do not exist yet. Protection also blocks moving
or deleting their ancestors, and applies to storage cleanup. Read access remains
available; there is no owner bypass or protection registry framework.

Reads optionally select inclusive one-based lines and return their first line
number. Writes create or overwrite using a sibling temporary file and `os.replace`,
reporting whole-file removed/added line counts. Edits require a nonempty exact
`old_text`: zero matches or multiple matches without `replace_all` leave the file
unchanged. There are no revision hashes, optimistic-concurrency parameters, or
multi-file transactions. UTF-8 and existing line endings are preserved outside
the replaced text. Move supports files/directories and rejects existing destinations.
Delete supports files and empty directories. Mkdir creates missing parents.

Filename searches use a basename glob unless the pattern contains `/`, in which
case it matches a path relative to the search directory; `**` supports nested
directories. Text search uses the `regex` package, is line-oriented, and returns
one result per matching line, with `spans` containing zero-based Unicode-code-point
[start, end) pairs produced by the same regex engine, including zero-width matches. Both searches skip dot directories and links but
include ordinary build directories and dot files. Only text search has an extension
filter; filename search uses its glob alone. Extension filters accept a
leading dot and compare case-insensitively. Binary/non-UTF-8 files are reported in
`skipped_files`. By explicit design, workspace scans currently have no application
timeout, output cap, index, or pagination.

### Service storage and future consumers

The default storage root is `.<project-name>.forgemcp` beside the project; the CLI
can override it with `--workspace-storage`. It is created lazily. Storage must not
equal or contain the project root. `storage_directory(key)` returns a small
`StorageDirectory` with `path`, `subdirectory(key)`, and `remove()`. Persistent
directories survive restarts. `temporary_directory(prefix)` is a context manager
for a unique directory below storage/tmp; it removes that directory on exit.
The caller must stop processes before deleting their directories.

CMake can request `storage_directory("build").subdirectory("cmake-debug")`;
other consumers can request persistent storage directories. Workspace knows neither build
configuration nor index lifecycle. Git can register `.git` with `protect_path`
in its constructor. ProcessService receives plain allowed root paths, not a
dependency on WorkspaceService. Its check restricts cwd, not OS-level file access.
Watchers, Git change callbacks, and language-server synchronization/highlighting
are deferred until those consumers are implemented.

### Workspace resources

Six file templates use one qualified `path`, including `project/` or `storage/`:

```text
forgemcp://workspace/file{/path*}
forgemcp://workspace/raw{/path*}
forgemcp://workspace/list{?path,depth,include_hidden}
forgemcp://workspace/find-files{?pattern,path}
forgemcp://workspace/file-info{?path}
forgemcp://workspace/search{?query,path,regex,extensions,case_sensitive}
```

Text mirrors return complete UTF-8 text with `text/plain`; raw mirrors return exact
bytes with `application/octet-stream`. The other four render the same business
results as `text/markdown` through the shared Markdown helpers. Embedded code fences
are escaped by choosing a longer fence. No per-file resource registration or cache
is needed. Mirrors read current files regardless of search exclusions; tree and search
resources use the same filtering policy as their tools.

Query parameters use RFC 6570 percent encoding, including `%20` for spaces; `+`
remains a literal plus. Resource depth `all` maps to tool depth null. Text-search extensions use
a comma-separated string; an omitted value means any extension, an empty value
means extensionless files. Regex works in URI parameters. SDK path-security checks
are exempted only for `query`/`pattern`, which are data; actual paths still pass SDK
and workspace checks. Path completions first suggest `project/` and `storage/`, then qualified child
paths. Directory inputs suggest directories; file inputs also suggest files.
Extension completions scan the directory selected by `path` in completion context.
Completions also cover depth and booleans;
only completion responses observe the protocol's 100-value cap.

### Workspace result extensions

`register_extension(name, provider, tools=None)` registers an async provider under
a unique name. An optional tools sequence limits its invocation. Each tool passes
an `ExtensionContext` containing a result copy, qualified paths, already-read
texts, and an optional immutable `TextChange`. Write/edit capture the successful
  mutation in the MCP handler at the call site; snapshots stay out of
public results. The diff provider is scoped to write/edit, so searches do not
retain full texts for it. Providers must not change files.

A provider returns `ExtensionOutput(resource, metadata=...)` or None. Its primary
`ExtensionResource` supplies a unique .json/.md filename, matching MIME type, and
text. Workspace validates JSON and stores immutable resource text. The result's
`resources` mapping uses provider names as keys, each containing a typed descriptor
with `uri`, `mime_type`, and optional JSON metadata. Metadata cannot override the
link fields and is detached from provider-owned mutable values before another
await. A failed provider publishes no descriptor and logs only a lifecycle summary;
the successful file operation is preserved. Cancellation still propagates.
There is no separate manifest or `extensions_uri`.

```text
forgemcp://workspace/results/{result_id}/{name}.json
forgemcp://workspace/results/{result_id}/{name}.md
```

`workspace/diff.py` is explicitly registered in `server.py` as diff. It publishes
`resources.diff` with URI, application/json MIME type, and version 1. `diff.json`
is a typed `FileDiff`: qualified path, compact change ranges, and hunks with three
context lines. Each hunk contains typed context/added/removed lines with nullable
before/after numbers, exact text (including line endings), and character spans.
Spans use zero-based Unicode code-point [start, end) offsets; zero-width spans mark
insertions/deletions. Replacement lines are paired in their original order for
character comparison; unpaired added/removed lines are wholly changed. Starts are
one-based; a zero count denotes the next insertion position. Identical text produces
empty changes/hunks. Providers use captured snapshots, never subsequent file reads.

The App bridge reads resources.diff directly, restricts it to workspace result
JSON URIs, checks its MIME type/version, and validates decoded data. It ignores
late reads after input, cancellation, or teardown. Rendering receives data without
transport access and displays two number columns and change markers, without
unified-diff headers or a redundant field label. Highlight changes toggles
character-span emphasis next to Wrap lines; search highlighting is independent.
JSON and Copy all retain the original tool result.

Published resources remain immutable in memory until shutdown; reads never rerun
providers. Unknown IDs/names raise ResourceNotFoundError. No resources are created
when no provider contributes. Persistent result storage remains deferred.

## CMake profiles and operations

`CMakeService` receives project/storage roots and protected paths directly, plus
`ToolchainService` for tool selection. It owns its build-tree filesystem operations.
Operator profiles are parsed from repeated
`--cmake-profile NAME KEY=VALUE ...` arguments; the default
toolset is selected by `--cmake-toolset`. There is no environment-based profile
configuration, persistent profile registry, or directory lock.

Without explicit profiles, projects with a presets file receive one `presets`
profile containing all available configure/build/test presets. Names come from
the selected CMake's `--list-presets=all`; CMake handles hidden presets, conditions,
includes, inheritance, macros, and environment settings. ForgeMCP only reads its
textual name listing. Without presets files, Debug and Release profiles use
separate directories under `storage/build/`.

Explicit profiles bind a toolset and either native preset names or ordinary
configure settings. Native configure uses `cmake --preset`, build uses
`cmake --build --preset`, and test uses `ctest --preset`. ForgeMCP does not resolve
or constrain native `binaryDir`. The bound toolset environment is supplied to
CMake/CTest, which then applies preset environment rules. A missing build/test
preset requires an explicit `build-directory` for that operation; this fallback
does not reconstruct a configure preset's environment. Plain profiles accept
generator, compiler tool names, a WorkspacePath toolchain file, and cache definitions.
Their source is always the project root and their build path passes workspace checks.
Plain profiles default to Ninja; its executable is taken from the chosen toolset.
Missing Ninja is an error, not a reason to change generators. For a plain profile,
changing the generator completely removes and recreates its build directory.
Settings and cache ownership are checked first. CMake checks roots, protected
subtrees, and links before removing its build directory. Native presets are unchanged.

`cmake_profiles` lists the effective profiles. `cmake_configure`, `cmake_build`, and
`cmake_test` run all profiles unless a subset is supplied. Operations run sequentially,
report failures per profile/preset, and continue other profiles. Repeated native
configure presets within one call are configured once. Existing plain build caches
must belong to this project, and build/test configurations must match their cache.
Known plain build directories receive CMake File API queries and request a compilation
database. Native presets retain control over these settings and directories.

Commands are typed methods on bound CMake/CTest ToolSpecs and launch only through
ProcessService. Callable protocols preserve their positional and keyword signatures.
Tool files own process consumption and parsing: CMake presets/cache/configure/build
output lives in `toolchain/tools/cmake.py`, and CTest progress/JUnit parsing lives
in `toolchain/tools/ctest.py`. Methods return typed parsed results, never sessions.
Each invocation defaults to a 600-second total timeout. Progress callbacks carry
configure steps, build actions, and test case status; the MCP layer adds the profile
name and applies the common throttle. Its monotonic counter counts status updates,
without an invented percentage across multiple commands. Each MCP operation returns
a list of its own result model: `CMakeConfigureResult`, `CMakeBuildResult`, or
`CMakeTestResult`. `error` is the sole outcome field (null on success); there is no
batch status, repeated operation name, or separate exit-code field. Configure adds
known build/compilation-database paths, build adds parsed step counts, and test adds
JUnit cases. Results carry `process_id` instead of copied output; use `process_get`
for command logs. ToolSpec results retain exit codes for the service to interpret.
Execution/parser failures after launch also retain the process identifier. CTest writes
JUnit into a CMake-owned temporary directory under storage; parsed cases are returned
before cleanup.

CMake registers all four tools through Apps with separate packaged widgets:
profiles, configure, build, and test. They share `cmake-view.js` and the common
result renderer: profile/mode filters, Fields/JSON, full-value copying,
process identifiers for log retrieval, and expandable test cases. Fields used as
record headings are not repeated in the body; JSON and copying retain all fields.
The widgets display the original
invocation only and issue no tool calls or resource reads. CMake highlighting remains deferred.
Unit and in-process MCP tests cover operator profiles, parsed command results,
generator-change cleanup, error isolation, immutable extensions, qualified resource
paths, and completions. They use fake ToolSpecs/process streams and isolated fixture
copies; installed compilers are not required. Existing workspace widgets have not
yet been adapted or verified against the new WorkspacePath/extension contract.

## External process execution

`ProcessService` is the only module that calls `asyncio.create_subprocess_exec`.
Feature services receive it through constructor injection, select executables, and
construct explicit argument lists. `launch` uses exec mode; interpreters such as
`cmd.exe` must be selected explicitly by the owning feature. ForgeMCP does not expose
a generic command-execution MCP tool.

`launch` supports `inherit_environment=True`, which
copies the server environment then overlays `env`. With `False`, only `env` is passed
to the child. No environment values are added to operational lifecycle logs.

`async with await processes.launch(...) as session` is the shared API for short
commands and long-running sessions. Launch starts a supervisor coroutine owned by
the session. Its TaskGroup owns stdout and stderr readers, a stdin writer, and a
monitor. Worker failures do not cancel the consumer directly: the supervisor first
cleans up the process, then exposes the retained failure through `output()`, `wait()`,
or an otherwise successful context exit.

`output()` has one consumer and yields `ProcessOutput(stream, text)` chunks from both
pipes through one currently unbounded queue. It ends after both pipes reach EOF;
failed reads and timeouts are not successful EOF. Cross-stream ordering is the order
ForgeMCP observed, not an operating-system ordering guarantee. Unconsumed output and
the full transcript remain an in-memory retention limitation.

`write_stdin(text)` queues text for the stdin writer without waiting for transport
drain. The queue carries `str | None`; `close_stdin()` queues `None` after prior
writes and is repeatable. `wait()` waits
for process exit and completed readers, returns the exit code, and can be repeated;
it does not implicitly close stdin. Nonzero exit codes are interpreted by the tool
module. `close()` and context exit stop a remaining process and await all workers.
`ProcessService.close()` closes remaining sessions during server shutdown. The
session exposes `process_id` and `returncode` without exposing its queue protocol as
the consumer API.

Each pipe owns an incremental decoder so a multibyte character split across OS reads
is reconstructed correctly. The default encoding is `locale.getencoding()` and each
consumer may select an explicit encoding. Replacement decoding is the resilient
default for human-facing tool output; strict decoding is available for formal
protocols. All consumer I/O is text. A tool can choose strict `latin_1` for a reversible
one-character-per-byte representation, then frame and decode protocol messages in its
own module. Such transcripts retain that Latin-1 representation, not decoded UTF-8
protocol text. There is no separate raw mode or protocol session class.

Every stdin write and stdout/stderr read is appended to a process transcript in
memory. The record also retains identifiers, command metadata, lifecycle timestamps,
encoding, timeout policy, state, and return code. This is inspection state, not an
operational log: stderr logging contains only process identifiers and lifecycle
summaries and never copies transcript content or environments.

`ProcessTimeout(total=None, idle=None)` disables both timeouts. Each non-null field
sets seconds for its limit; total and idle may be enabled together. Total time uses
the monitor's monotonic start time. Idle measures time since the latest transcript
entry, including stdin. Timeout raises the existing `ProcessError` after termination;
stream failures use `ProcessStreamError`.

On timeout or explicit termination, the directly managed asyncio process receives
`terminate()`, followed by `kill()` if it remains alive after a short grace period.
Cancellation of an individual I/O or wait operation does not close the session.
Explicit `close()` and context exit await cleanup, including during cancellation.
Launch directly awaits `asyncio.create_subprocess_exec` without a separate task or
custom cancellation handler. Cleanup after worker failure drains unread pipes without
retaining the discarded tail. Process
groups and descendant-tree management are intentionally outside the current scope.

The read-only MCP surface consists of `processes_overview(status)`,
`process_get(process_id)`, their widgets, `forgemcp://processes`, and
`forgemcp://processes/{process_id}`. Overview entries pair `ProcessSummary` with a
transcript-free `ProcessStatus`; `current_status` is a return code, `running`,
`interrupted` (timeout), `stopped`, or `stream_failure`. The complete transcript stays
on `ProcessRecord` and is sliced by the detail tool according to the requested limits. The detail view derives
elapsed seconds from each entry's timestamp and the process start timestamp. The
detail tool exposes a point-in-time state and selected ordered text fragments;
the resource retains the full transcript. Neither starts or stops a process. The detail widget provides combined
and per-stream views, with basic ANSI SGR color
rendering. Completion suggests retained process IDs.
A well-formed URI whose process ID is not retained raises
`ResourceNotFoundError`; other unexpected exceptions remain unwrapped so the SDK
sanitizes them as resource crashes.

## Toolset discovery and command execution

`ProcessService -> ToolchainService` is wired explicitly in `server.py`. The lifespan
awaits `ToolchainService.initialize()` before requests; an initialization lock prevents
duplicate discovery. A complete sorted tuple is published atomically and retained
for the server lifetime. There is no automatic refresh, watcher, global selection,
service locator, or plugin framework.

`loader.py` enumerates `forgemcp.toolchain.tools` with `pkgutil.iter_modules`, skips
private names, sorts module names, and requires exactly one valid `INFO: ToolInfo`
per module. `ToolInfo` contains the logical name, kind, and `create_spec` callable. Duplicate logical names and invalid modules raise domain errors. Adding a
built-in module automatically enables system and user discovery; VS-specific paths
are added only in the VS provider.

Providers own platform policy. System uses executable PATH lookup, including OS
suffix handling, and creates one possibly empty toolset. Visual Studio uses the
Installer's `vswhere`, checks shallow known installation layouts without PE inspection,
and captures each instance's `VsDevCmd` environment through `cmd /c call ... && set`.
The entire mapping is retained without an allowlist or per-value limits. Every such
process uses `ProcessService`; its complete output remains in the normal transcript.
One broken instance cannot suppress others. A failed environment capture leaves that
instance visible with no bound tools, avoiding execution with an incorrect environment.

Repeated CLI definitions are validated before discovery launches processes. Names
and tool keys cannot repeat; paths must identify executable files, including inside
the workspace. User toolsets inherit the server environment and never fall back to
PATH on configuration errors. Discovery creates specs without querying tool versions.

`ToolSpec` is created with its name, kind, executable path, and `methods` dictionary.
It has no partially initialized state, bind/replace step, or stored version. Providers
call `ToolInfo.create_spec(path, processes, environment, inherit_environment)` after
finding an executable. Toolsets contain ready specs and their environment; the service
contains the discovered toolsets. `list_toolsets()` and `get_toolset(id)` return these
containers, and `get_tool(id, name)` returns a spec or None. There are no `selected`,
`current`, or `preferred` fields.

Each tool module declares its own `Methods` TypedDict and implements callables locally
inside `create_spec`. Toolchain sees only an optional, read-only `version` callable in
`ToolMethods`; concrete consumers import the tool module's `Methods` and cast
`spec.methods` to that type. No inheritance between these TypedDicts is required.
Argument conversion, process execution, and parsing stay inside the tool module.
There is no generic ToolCommand, parser registry, or CommandExecution wrapper.

Current implementations provide only version methods, with a 15-second process limit
and a small retained banner from each output stream. They consume both streams and
check exit status before returning a parsed version string. `cl`, `link`, `cppvsdbg`,
and `lldb-dap` currently have empty method dictionaries. Longer sessions can later be
exposed through explicitly typed iterator or context-manager methods without changing
the containers.

Only MCP detail handlers call `methods["version"]()` when available. Versions are not
cached: each details request obtains fresh values; unsupported, failed, or unrecognized
versions appear as null in structured output. Pydantic response models omit toolset
environments and expose absolute executable paths. Resource documents use the shared
`markdown` module; the former manual Markdown formatting helpers are removed.

`toolsets_list` returns summaries (the SDK wraps a list in `structuredContent.result`),
and `toolset_get` returns one details object. Both report progress and carry read-only
annotations, icons, Apps metadata, and SDK text fallback. The toolsets widget displays
only the supplied result: summaries for a list call, or details of one toolset for a
get call. It never requests details or selects server-wide state. The static list
resource and details template return markdown, and completion
offers discovered toolset IDs. Unknown IDs become ToolError or ResourceNotFoundError at
the corresponding boundary; unexpected exceptions remain SDK-sanitized.

## Clangd business API

`ClangdService` receives `WorkspaceService`, `ToolchainService`, and `CMakeService`
explicitly. `server.py` registers its seven MCP tools and workspace extension,
and closes the analysis service before `ProcessService`. Widgets remain deferred.

CMake retains successful configurations as
`CompilationContext(id, toolset_id, build_directory, compilation_database)`.
It starts from existing compilation databases, updates entries after configure,
and drops entries whose databases disappear. Build directories come from profile parameters;
a single native preset can use an explicitly configured build directory (`-B`).
Unknown directories are omitted rather than parsing command output or guessing
CMake preset expansion. Clangd additionally excludes contexts whose toolset lacks clangd.
Empty configuration selections mean all available contexts; executables never fall
back to another toolset or a system installation.

`configuration_updates()` is CMake's in-process subscription stream. It yields the
current list immediately and then yields changed snapshots when configure adds or
removes a configuration. A slow subscriber keeps only the newest pending snapshot;
it does not delay CMake or receive events for unchanged lists. Subscribers close
their generators to unregister.

The typed clangd ToolSpec method `connect` owns `ProcessSession` and JSON-RPC/LSP
framing. Callers exchange typed request, notification, response, and error envelopes.
UTF-8 message bodies are framed by byte length without an added frame-size limit.
The process text transport uses reversible Latin-1 encoding for those bytes; its
transcript is therefore not a decoded UTF-8 LSP log. Clangd uses its normal index
locations, including the cache beside the compilation database.

`ClangdSession` handles initialization, request correlation, cancellation, document
versions, diagnostics, and shutdown. Progress callbacks reach the session layer.
Positions use one-based lines and zero-based Unicode code-point offsets. The session
requires UTF-32 position negotiation so external locations can be returned without
reading their contents to convert offsets.

`ClangdService` lazily retains one session per selected context. It implements
diagnostics, hover, definition, references, document symbols, and workspace symbols.
Each operation has its own result model with `configurations: list[str]`. Equal
complete answers are grouped with their configuration IDs; differing answers remain
separate. An analysis failure raises a domain error rather than silently returning
an incomplete multi-configuration answer. Changed compilation databases, failed
sessions, and missed workspace revisions invalidate retained sessions.

The workspace extension supports read, write, edit, move, and delete results. It
updates active sessions after mutations and captures version-matched diagnostics
and semantic highlighting into the existing immutable result-resource mechanism.
It does not watch external edits or manage editor buffers. Workspace mutations stay
successful if enrichment fails, following the existing extension contract.

`WorkspacePath` can also represent external absolute locations with `root/`, for
example `root/C:/SDK/include/header.h` or `root//usr/include/header.h`. Managed
filesystem operations reject this area. `workspace_read_file` uses an injected
`confirm` parameter with SDK `Resolve`/`Elicit`: managed paths need no question,
external paths require explicit approval, and declining or cancelling stops the
read. The confirmation is requested for each external read; no separate permission
service is used. Clangd's own include reads do not require these confirmations.
External diagnostics are excluded, but symbol locations may reference external files.
The path type exposes an inline JSON Schema string, without a `$ref` indirection.
External files never have text/raw mirror resources, even after reading is approved;
resource requests for them raise `ResourceNotFoundError`.

## MCP Apps and widget packaging

Each model-visible tool must bind exactly one `ui://` resource through
`_meta.ui.resourceUri`. The HTML is an optional human view; the tool result remains
complete for the model and text-only clients.

Widget source imports `@modelcontextprotocol/ext-apps`, installs handlers before
connecting, and reacts to host theme, style variables, fonts, and safe-area insets.
Vite plus `vite-plugin-singlefile` produces self-contained HTML under
`src/forgemcp/assets/`. `Widget` locates it relative to the installed package. The
Hatch wheel hook runs `npm ci` and the frontend build, then includes generated assets
even when they are ignored as build output. The frontend lockfile controls reproducible
UI builds.

Icons are separate files under `src/forgemcp/icons/`. `IconFile` converts a packaged
file to portable `data:` metadata at runtime, preserving offline operation without
hardcoded blobs in Python. Each tool still chooses an icon that identifies its
operation rather than relying only on the server logo.

### Shared widget implementation

Every widget uses the same compact console/TUI presentation. The current workspace,
process, and toolsets views share these files under `frontend/src/shared/`:

| File | Responsibility |
| --- | --- |
| `widget.css` | Geometry, host-aware palette, typography, controls, field grids, scrolling and syntax colors |
| `copy-icon.js` | Shared inline SVG copy icon, with no image requests or CSS masks |
| `presentation.js` | Pure formatting and process/toolset snapshot projections |
| `result-view.js` | Shared DOM renderer, local filters, Fields/JSON switch, tooltips and copying |
| `app.js` | Apps connection, result lifecycle and host context; imports the shared CSS |

Workspace's four HTML entrypoints use `workspace-view.js` for projections and
specialized tree/source values, passed through the shared renderer's optional
`renderValue` callback. File/source values carry line numbers without altering
the original text used by copying and JSON. Search/find results reuse the shared
collection filters. Metadata and mutation results use the shared fields view.

An HTML entrypoint contains `<main id="widget" aria-label="Descriptive name"></main>`
and one module script. Its JavaScript supplies the tool name and a pure projection:

```javascript
import { connectWidget } from "./shared/app.js";
import { processPresentation } from "./shared/presentation.js";

await connectWidget({
  toolName: "processes_overview",
  describe: processPresentation,
});
```

The projection returns `toolName`, `summary`, and `records`. Use `records: null`
for a field-oriented result. For collections, return the complete array, all other
top-level fields in `summary`, and `titleKey` plus an optional `categoryKey` for
record headings and local filtering. It must not modify or discard original fields.
The original structured object remains the source for JSON and copying.

Use the existing renderer for new results that fit these two forms. If a future
feature needs a specialized view, reuse `widget.css` and the shared controls and
formatters; add only its actual presentation needs. Do not copy a stylesheet into a
feature directory or introduce per-widget palettes, sizes or spacing. Change common
tokens and rules centrally so all widgets stay consistent. This is shared
presentation for concrete callers, not a widget registry or plugin framework.

### Visual and interaction contract

These requirements apply to all existing and future widgets:

- **Geometry:** the outer widget is always 420 CSS pixels high and fills 100% of the
  width supplied by the host, with no fixed or maximum width. Safe-area padding is
  included inside that height. Switching views, filtering, receiving a result and
  expanding data must not resize it. The content area scrolls vertically; header,
  controls and footer remain in place. No horizontal scrollbar.
- **Density:** use 13px monospace text with a 1.35 line height, compact rows, small
  control padding and thin neutral separators. Avoid large cards, generous blank
  space, rounded dashboard panels and oversized headings. Touch controls can have
  larger hit areas without increasing the outer widget height.
- **Palette:** neutral graphite surfaces in dark mode and neutral pale surfaces in
  light mode, with host background/text/border variables and host monospace fonts.
  Cyan marks active controls and JSON keys, amber marks booleans and warnings,
  lavender marks numbers, and muted green marks successful completion. Red marks
  `failed`, `timed_out` and nonzero exit codes; `terminated` is amber. Use color
  sparingly and retain the literal status text so color is never the only signal.
- **Complete text:** render every supplied field and record, including unknown
  fields, nulls, empty collections and backend flags such as `scan_truncated`.
  Do not add row/character limits, slicing, pagination caps, ellipsis, line clamps
  or hidden overflow that clips data. Long names, labels, paths and JSON wrap within
  their column and remain selectable. Key/value columns must have independent
  wrapping and a gap: `had_decoding_errors` cannot overlap its value. Backend scan
  limits are a separate contract; show the returned truncation flag explicitly.
- **Readable fields:** scalar arrays appear as compact comma-separated wrapping
  lists; objects use nested labeled values, not serialized JSON pasted into a cell.
  Preserve all entries and distinguish empty list, empty object, empty string and
  null. Process timestamps show a readable date/time to whole seconds with explicit
  UTC and ISO 8601 in parentheses, for example
  `7 Sept 2026, 12:45:12 UTC (2026-09-07T12:45:12Z)`. Display conversion does not
  alter the original timestamp, precision or offset in JSON/copy.
- **JSON:** provide a syntax-highlighted JSON view of the complete original
  structured result, unaffected by local filters. Highlight keys, strings, numbers,
  booleans, null and punctuation. Insert untrusted text with DOM text nodes, never
  interpret result strings as HTML. Line wrapping must preserve selectable content.
- **Local interaction:** allow category filters, view switches, tooltips,
  keyboard navigation and copying. Show matched/total counts and an explicit
  no-match state. Filters are reversible and have no effect on the source result.
  All supplied records are visible by default; none is silently omitted.
- **Copying:** provide small labeled copy icons for individual original values
  and a Copy all action for the entire original result, including filtered records.
  Render the icon as inline SVG with a visible `currentColor` stroke even before
  hover. Do not use CSS image masks: embedded hosts may block their image URLs.
  Copy strings verbatim and objects/arrays as JSON. If clipboard permission is
  unavailable, try the local selection fallback; if that also fails, select the
  complete original value for manual copying and explain the keyboard shortcut.
  Never claim a copy succeeded when it did not.
- **One invocation:** widgets display one particular tool invocation's
  `structuredContent`. They may repeatedly read immutable resources linked by that
  result under `forgemcp://workspace/results/*`. Other resource reads, tool calls,
  external fetches, polling, and refresh buttons are not allowed. Resource loading
  belongs in the App connection code, which passes decoded data to rendering code.
  The shared renderer receives no App or
  transport object. A `toolsets_list` result contains summaries only; detailed
  fields appear only when `toolset_get` itself supplies them. Interactivity does
  not authorize additional MCP calls beyond these result-resource reads.
- **Lifecycle and accessibility:** register handlers before `app.connect()`,
  apply initial and changed host theme, fonts, style variables and safe-area insets.
  A widget remains bound to its original invocation; it is not reused for a later
  call. Ignore late clipboard completion after teardown. Show useful waiting,
  empty, error and missing-structured-data states; do not parse text fallback into
  invented structured data. Use semantic controls, visible focus, accessible icon
  labels and copy/status feedback. Load no external assets; host fonts are applied
  through the SDK. Useful MCP text fallback remains independent of the widget.

When adding a widget, wire its source into the Vite build and Python `Widget` binding,
add representative DOM tests, and verify its generated HTML and icon are packaged.
Tests should cover full values, extra fields, long paths, local filtering without
data loss, JSON escaping, copying, empty/error states and teardown.
Run `npm test --prefix frontend` and `npm run build --prefix frontend`; these use
Node/jsdom and Vite without launching a browser. DOM tests do not prove pixel layout;
visual review remains a separate step.

## Errors and trust boundaries

Expected business failures use exceptions from the feature's `errors.py`. MCP-facing
methods translate them according to who can recover:

- `ToolError`: the model can change arguments or sequence and retry;
- `ResourceNotFoundError` or another MCP error: the resource/request cannot be served;
- any other exception: unexpected bug, logged server-side and sanitized by the SDK.

Project-controlled text is data. It must not change server instructions, choose an
executable, escape the workspace, or be copied into operational logs. Process
consumers must explicitly select executables, construct arguments, choose timeout and
encoding policy, and handle cancellation. The current process transcript is retained
in memory; persistent storage and configurable retention limits remain future work.

## Testing strategy

Business tests call service methods directly. The function-scoped
`cpp_acceptance_project` fixture copies the complete
`examples/cpp-acceptance-project/` tree into pytest's `tmp_path`, giving every test an
independent workspace it may modify. Existing scenarios should use this shared fixture
instead of constructing ad-hoc C++ directory trees.

Protocol tests use the SDK's in-process `Client` and the same isolated workspace to
assert the public contract: schemas, Apps metadata, icons, progress, structured
output, resources, prompts, and completions. The source acceptance project remains a
portable fixture for later checks against real CMake, compilers, clangd, sanitizers,
and debuggers; tests never operate on it in place.

`.\.venv\Scripts\python.exe -m build --wheel` is the release build: it builds the
frontend and Python wheel together. Python changes must also pass
`.\.venv\Scripts\python.exe -m pytest -q`, and the built wheel must contain every
referenced widget and icon. VS Code's `ForgeMCP: server` launch configuration invokes
the build task before starting the server.

## Expected next modules

The likely order is:

1. Refactor Workspace result providers around before/after/error notifications and
   sequential Workspace MCP entrypoints. Move tool-specific filesystem work into
   the decorated handlers. CMake already owns its build-tree filesystem work;
   process launches continue through ProcessService.
2. clangd validation and widgets;
3. formatting, static analysis, and sanitizer parsing;
4. debugger adapter lifecycle and DAP operations;
5. persistent process transcripts and configurable retention limits.

This ordering is guidance, not a framework contract. Add the smallest end-to-end slice
needed by the next user-visible workflow.

### Process transcript selection

`ProcessRecord.append_log` records decoded text under its lock and maintains a
single LF-based line cursor across stdin/stdout/stderr. Each immutable entry has
inclusive `start_line`/`end_line`; a line spanning chunks overlaps their ranges.
Empty text does not create an entry, and a trailing LF creates no phantom line.

`process_get` snapshots and selects under the same lock. Its selectors are unions:
`FirstLines | LastLines | LineRange` and `FirstSeconds | LastSeconds | TimeRange`.
Each variant requires its own fields and forbids other fields; ranges require both
bounds. JSON remains `{"first": N}`, `{"last": N}`, or `{"start": N, "end": M}`. Time is monotonic elapsed process time. Completed processes use the later
of their recorded duration and final entry time for tail selection, so drained
output remains available. Explicit time intervals are half-open.

`process/transcript.py` implements time filtering, line selection over merged
entry ranges, and a UTF-8 text byte budget. It slices only returned fragments and
preserves their timestamp/stream and absolute line numbers. It does not merge or
rewrite stored chunks. Byte clipping retains a prefix or suffix according to the
line selector (or time selector when lines is absent), preserving Unicode characters.
Responses add only the returned inclusive `lines` range, null when empty. No
truncation flags or total counters are added. Without selectors the default is
last 100 lines; the default text budget is 64 KiB. The full journal stays in memory.
The shared widget view omits the generic text filter and repeated heading fields.
Adaptation of the process widget to sliced logs remains deferred.
