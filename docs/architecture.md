# ForgeMCP architecture

## Status

ForgeMCP is intentionally at foundation stage. The `workspace` feature manages
project files and a separate service-storage root. The shared `process` service adds asynchronous
external-program lifecycle, text transcripts, timeouts, and a read-only inspection
surface for development commands. CMake now configures, builds, and tests operator
profiles, and directly runs explicit test executables with command-line arguments.
Read-only clangd analysis is mounted through MCP tools and workspace
result providers, with a shared analysis widget. Quality and debugger remain future work.
The `toolchain` service discovers independent toolsets once at startup and exposes
their paths and on-demand versions through a read-only MCP surface.
CLI instances also join a same-user local administration Hub by default. Each
instance retains its own services and volatile call history; the Hub hosts one
browser panel and routes commands back to the selected instance through local IPC.

The design goal is a small composition root plus independent feature services. There
is no plugin system, service locator, event bus, repository layer, or transport-neutral
adapter hierarchy. Add one only when concrete behavior makes it necessary.

## Source layout

```text
src/forgemcp/
  server.py                     # composition root, CLI, MCPServer
  assets.py                     # package-relative Widget and IconFile helpers
  completion.py                 # one server-wide completion dispatcher
  admin/
    history.py                  # SDK middleware and in-memory tool-call records
    instance.py                 # one SDK client and reconnecting instance IPC bridge
    service.py                  # shared loopback HTTP panel and instance routing
    errors.py                   # expected administration request failures
  local_ipc/                    # same-user Windows pipes / Unix sockets and framing
  assets/*.html                 # generated single-file widgets
  icons/*                       # source icon files packaged with the server
  <feature>/
    service.py                  # business logic, service class, MCP handlers
    errors.py                   # expected feature errors
    text.py                     # plain-text presentation where substantial
  process/
    service.py                  # subprocess lifecycle, streams, state, MCP inspection
    host.py                     # detached operator programs and OS process identities
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

1. `create_runtime` resolves operator configuration such as the workspace root.
2. Construct `WorkspaceService`, then `ProcessService` with the project and storage
   directories as allowed working roots, then feature services with explicit dependencies.
3. Create one `Apps` instance.
4. Create one `Complete` completion collector.
5. Construct `MCPServer(extensions=[apps], middleware=[history])`, composing its
   instructions from the base text and every service class docstring.
6. Call each service's single `register(mcp, apps, complete)` method.
7. Mount the validated Apps tools and resources through their public bindings and
   register the one server-wide completion handler.
8. `ServerRuntime.run_stdio` holds the application lifespan while stdio and the
   optional persistent administration client use the same services. The workspace
   defaults to the server process working directory. The first lifespan user awaits
   toolchain and clangd initialization; reference-counted users share that completed
   initialization. Only the final user closes analysis and managed processes,
   including cleanup after discovery failure.

`create_server` remains the embedding entrypoint returning the SDK `MCPServer`;
it does not start an administration Hub. The CLI creates a runtime and enables its
admin bridge unless `--no-admin` is supplied. The internal Hub CLI branch runs
before feature construction, so the Hub owns no workspace or toolchain services.

Steps 3-7 are deliberately visible. MCP Python SDK 2.1 fixes extensions at server
construction and consumes their bindings at that point. The empty `Apps` extension is
therefore supplied to the constructor for capability negotiation, then the bindings
collected by service registration are mounted through the SDK's public APIs. This
keeps one feature registration method without a generic plugin framework or private
SDK access.

A service may depend on another service, but receives it in `__init__`. It never
creates that dependency itself. This keeps tests local and makes application state
ownership visible in `server.py`.

The lifetime lock coordinates service initialization and final shutdown only.
It does not serialize tool execution. Workspace keeps its existing reentrant
operation lock, and clangd coordinates its analysis and Workspace provider hooks
through its own lock. CMake's operation lock serializes configure/build operations
and the Workspace provider's before-to-after/error interval; its publication lock
protects subscription snapshots. There is no global command queue or cross-instance
coordination of a shared source/build directory.

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
        async def example_tool(
            value: str,
            ctx: Context,
        ) -> Annotated[CallToolResult, Result]:
            """Describe the tool; the SDK infers its public metadata and schema."""
            ...
```

Only methods that the feature actually needs are present. The nested MCP entrypoints
close over the long-lived service instance, so intentional state survives calls while
the class remains focused on reusable business operations. The SDK derives names,
descriptions, schemas, and structured output from the entrypoint signatures and
docstrings. A local completion function returns `None` for references the feature does
not own and is added to the shared `Complete` collector.

Tool handlers explicitly return `Annotated[CallToolResult, ResultType]`. A small
shared `text.tool_result` helper serializes result models in JSON mode with aliases
and constructs the SDK response, while each feature owns its readable text.
Workspace and CMake result models live in `models.py` because both operations and
presentation use them; clangd and process extend their existing shared models.
Text has no Markdown wrapper, headings, or tables. Workspace reads contain only
numbered source lines, searches omit match spans from text, and provider resource
links remain exclusively in structured data. Process command lines quote argument
boundaries for display and do not execute through a shell. Clangd hover Markdown
is parsed with `markdown-it-py` into plain text while code and plaintext fragments
remain unchanged. Widgets continue to consume the original structured shapes.

## Current MCP surface

### Workspace files

`WorkspaceService` owns path checks, directory trees, metadata, UTF-8 reading,
literal/regex searching, mutations, and storage directories. The ten tools are
`workspace_list`, `workspace_find_files`, `workspace_file_info`, `workspace_read_file`,
`workspace_text_search`, `workspace_write_file`, `workspace_edit_file`, `workspace_move`,
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
work units. Typed result models are shared with feature-local plain-text renderers.
Tools return `Annotated[CallToolResult, ResultType]`, preserving SDK output schemas
and validation while explicitly supplying English plain text and JSON-compatible
structured data. List results keep the existing `{"result": [...]}` envelope.
Workspace Markdown resources validate the structured data from their internal tool
calls back into result models before rendering. Provider hooks still receive models,
and text is formed only after their result links have been attached.
Expected `WorkspaceError` failures become `ToolError` or `ResourceError` at the
corresponding boundary. OS errors retain the system-provided `strerror`, which may be localized. Resource roots are explicitly checked before filesystem access,
including mirrors without a path, to avoid opaque SDK validation errors. Platform-specific file ownership is isolated in
`workspace/metadata.py`; unavailable owner or creation time is null.

Project/storage paths shared between modules and in tool arguments/results use
`WorkspacePath`, serialized as a string such as `project/src/main.cpp` or
`storage/build/debug`. The roots themselves are `project/` and `storage/`.
`WorkspacePath.__new__` normalizes tool input before validation: an ordinary relative
path or one beginning with `./` selects `project/`, and one trailing `/` on a
managed non-root path is removed. `./` selects the project root; empty input remains
invalid. Explicit `project/`, `storage/`, and `root/` prefixes retain their meaning;
`./storage/...` denotes a project directory named `storage`. Stored and serialized
paths are always qualified and canonical. External `root/` paths are not normalized;
double separators, remaining `.` components, and `..` are rejected.
Executable locations and low-level filesystem APIs still use native `Path` values.
There is no separate `root` tool argument. Path traversal,
absolute paths, and resolutions outside the selected root are rejected. Trees
display symlinks/junctions without descending into them. Trees hide dot-prefixed
directories by default (`include_hidden=true` includes them); dot files stay visible. Explicit reads may follow
in-root links; mutations reject linked path components. Moving a directory
containing links is rejected. Workspace roots cannot be
mutated. `protect_path(WorkspacePath(...))` lets dependent modules protect files or
directories, including paths that do not exist yet. Protection also blocks moving
or deleting their ancestors. Read access remains
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
`skipped_files_count`, with the full list linked as `resources.skipped_files` to an
immutable JSON snapshot. Workspace reuses `save_result_resource` and
`read_result_resource` for this owned resource; its name is reserved against provider
registration, and it is saved after provider finalization so their cleanup retains
its independent lifecycle. Text search accepts positive `max_matches` (default 100)
and retains at most that many matching lines from traversal, sorted for output.
Scanning continues to compute exact `matches_count` and `skipped_files_count`;
`matches_truncated` reports omitted matching lines. There is no per-line byte cap,
application timeout, index, or pagination. Filename searches remain unbounded.

### Service storage and future consumers

The default storage root is `.<project-name>.forgemcp` beside the project; the CLI
can override it with `--workspace-storage`. It is created lazily. Storage must not
equal or contain the project root. Each feature creates and removes its own
directories under the injected storage root; Workspace exposes no storage-directory
wrapper or filesystem tool methods for other modules. Persistent directories
survive restarts. The owning module must stop processes before deleting their
directories. Workspace knows neither build configuration nor index lifecycle.
Git can register `.git` with `protect_path`
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
forgemcp://workspace/search{?query,path,regex,extensions,case_sensitive,max_matches}
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

### Workspace result providers

Each Workspace tool implements its filesystem operation in the local decorated
MCP handler. It calls `before_providers` with every operation argument, then
`after_providers` with its typed result on success or `error_providers` when the
operation raises, including cancellation. Expected Workspace errors become
`ToolError`; unexpected failures retain the SDK's sanitization. The handler
attaches the saved resource descriptors after all providers finish. Providers
receive no progress callback and cannot change the operation's result or flow.
Shared low-level UTF-8 reading and atomic replacement helpers serve multiple
Workspace callers; there are no public service methods implementing file tools.

`ResultProvider[ContextT]` has a typed `before_workspace_*` and
`after_workspace_*` pair for each Workspace tool. Before receives that tool's
arguments and returns provider-owned context; after receives the same context
and that tool's concrete result model. Default methods do nothing, with before
returning `None`; this skips its after and error hooks for that operation.
Providers override only operations they use. On tool failure,
`error(call_id, context)` receives each non-`None` before context.
If cancellation interrupts before while another provider is still running,
Workspace also calls error for providers that already returned a context.
If cancellation prevents an after hook from starting, its context is released
through error as well; hooks that did run own their cleanup.

Workspace uses one random ID for all providers in a tool call. It runs each stage
concurrently, waits for every provider, and logs provider failures without changing
the tool outcome. Each provider receives its own copy of the input arguments and
result. `register_provider(name, provider, tools=...)` limits which tool methods
are invoked. `save_result_resource(call_id, provider, mime_type, text)` stores
immutable JSON or Markdown under that ID and returns its URI. Workspace accepts
only a URI saved by the matching provider for that call. The result's `resources`
mapping contains provider names and descriptors with `uri` and `mime_type`.
There is no separate manifest or `extensions_uri`.

```text
forgemcp://workspace/results/{result_id}/{name}.json
forgemcp://workspace/results/{result_id}/{name}.md
```

`DiffProvider` from `workspace/diff.py` is registered in `server.py` for write/edit.
Before captures the previous complete file text; after reads the resulting text,
saves `diff.json` under the shared call ID, and returns its URI. The resource is a
typed `FileDiff`: qualified path, compact change ranges, and hunks with three
context lines. Each hunk contains typed context/added/removed lines with nullable
before/after numbers, exact text (including line endings), and character spans.
Spans use zero-based Unicode code-point [start, end) offsets; zero-width spans mark
insertions/deletions. Replacement lines are paired in their original order for
character comparison; unpaired added/removed lines are wholly changed. Starts are
one-based; a zero count denotes the next insertion position. Identical text produces
empty changes/hunks. Failed operations publish no diff resource.

Published resources remain immutable in memory until shutdown; reads never rerun
providers. Unknown IDs/names raise ResourceNotFoundError. No resources are created
when no provider contributes. Persistent result storage remains deferred.

Workspace serializes its MCP tool handlers, dynamic resource handlers, and path
completion handler with one lock. The lock remains held while a handler waits for
providers. A resource handler may call a tool handler in the same task without
deadlocking; requests from other tasks still wait. File-info, list, find, and
search resource handlers delegate to the corresponding tool handlers; file/raw
mirrors directly read their managed files.

Clangd registers a typed result provider alongside diff. The former extension
contract and Workspace revision counter have been removed. Resource descriptors
contain only URI and MIME type; provider-specific data belongs in the resource.
Widgets load diff and clangd from these descriptors independently through the App
bridge. Diff validates its concrete payload; clangd's resource version is inside
its JSON payload rather than the link.

## CMake profiles and operations

`CMakeService` receives project/storage roots and protected paths directly, plus
`ToolchainService` for tool selection and `ProcessService` for direct test runs.
It owns its build-tree filesystem operations.
Operator profiles are parsed from repeated
`--cmake-profile NAME KEY=VALUE ...` arguments; the default
toolset is selected by `--cmake-toolset`. There is no environment-based profile
configuration or persistent profile registry.

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

`cmake_run_test` runs one explicit `WorkspacePath` executable with literal
`arguments`, a `cwd` (default `project/`), and `ProcessTimeout` (default total 600
seconds). It uses the injected ProcessService directly and inherits the server
environment without selecting profiles, discovering tools, or replaying preset
environments. Executable and cwd must resolve inside their own project/storage
root; cwd must exist, executable must be a file, and Windows batch files are
rejected. NUL arguments are rejected while empty strings remain valid.
`execution_path` owns the shared path validation for executable and cwd.
The handler closes stdin, drains both output streams without accumulating another
transcript, waits for completion, and cleans up the session on cancellation.
Progress counts start/output/completion events with no invented total.
`CMakeRunTestResult` returns qualified command inputs, `error`, and `process_id`;
zero exit means success, while nonzero exits and process failures populate error.
Invalid paths or arguments raise ToolError before launch. A launched process's ID
survives timeout or stream failure for inspection with `process_get`.
CTest properties and framework case/JUnit reports are not interpreted by this tool.

CMake registers five tools through Apps with separate packaged widgets:
profiles, configure, build, test, and run-test. They share `cmake-view.js` and the common
result renderer: profile/mode filters, Fields/JSON, full-value copying,
process identifiers for log retrieval, and expandable test cases. Fields used as
record headings are not repeated in the body; JSON and copying retain all fields.
The widgets display the original
invocation only and issue no tool calls or resource reads. CMake highlighting remains deferred.
Unit and in-process MCP tests cover operator profiles, parsed command results,
generator-change cleanup, error isolation, qualified resource
paths, and completions. They use fake ToolSpecs/process streams and isolated fixture
copies; installed compilers are not required. Workspace/server tests still
target the former service methods and extension API; their migration follows the
widget work as the last refactoring stage.

### CMake configuration inputs and controlled mutations

`CMakeService` owns per-build-directory baselines and context eligibility.
`cmake/file_api.py` reads the latest successful index and its `cmakeFiles-v1`
reference, validates source/build ownership, bounds JSON replies and input counts,
and computes incremental SHA-256 hashes without retaining file contents. Configure
requests `cmakeFiles-v1` alongside codemodel/cache/toolchains, including native
presets with an explicitly known managed build directory. Unknown preset directories
remain outside context discovery; ForgeMCP does not expand native presets.
Each confirmed generation replaces that tree's complete dependency list, including
managed generated inputs. External inputs and links escaping managed roots are
excluded without reads or elicitation. This is an explicit boundary of freshness
tracking, not a guarantee about SDK/toolchain inputs in external `root/` locations.
The hashes and current/invalid state are in memory only. Startup never creates a
baseline by hashing files against an old reply.

`server.py` registers `CMakeProvider` beside diff and clangd for Workspace write,
edit, move, delete, and mkdir. Its typed hooks only delegate to CMakeService.
Before captures hashes for known dependencies beneath the affected paths and
path kinds for structural changes. It never scans a directory to find dependencies.
After verifies actual changes; error follows the same path to catch partial
mutations. Identical replacements preserve freshness. Shared inputs invalidate
all consuming trees, while ordinary C/C++ edits do not invalidate unless File API
listed them as configuration inputs. Root CMakePresets/CMakeUserPresets files,
including their initial absence, are conservatively tracked for every tree.
No inheritance/include resolver is implemented; preset inputs not identified by
File API or those known names are outside tracking.

cmakeFiles 1.1 exposes `globsDependent`. When nonempty, any managed structural
creation/removal/move conservatively invalidates that tree. With version 1.0,
structural mutations invalidate every such tree because glob presence is unknown.
No glob expression is interpreted or rescanned. Content-only edits of globbed
source files remain ordinary source edits unless also listed in `inputs`.
Missing files and changed contents invalidate through hashes, independently of
mtime. No operation auto-configures, removes stale artifacts, or watches external
changes. Configure and build also verify inputs at their controlled boundaries;
external editor/IDE/process changes are not guaranteed to be noticed.

A CMake operation lock serializes configure/build with each Workspace before-to-
after/error interval. Publication enqueues independent snapshots without awaiting
subscriber processing or taking the clangd lock. Clangd may hold its analysis lock
while waiting for CMake's mutation verification, then reconciles the current
snapshot before notifying sessions or collecting Workspace analysis. CMake never
waits in the opposite direction. Cancellation/error paths release the operation
lock and withdraw any tree whose freshness cannot be confirmed. An unchanged
context/database fingerprint retains its session; removed contexts close only
their own sessions. Old databases never restore an invalid/unknown tree.

Build compares File API generation identities before/after the command. Only a
new successful index confirms automatic regeneration and replaces dependencies;
this confirmation is independent of the build exit code, so compilation errors
after successful generation still publish the new context. A success exit with
an unchanged index cannot adopt current input hashes as a new baseline. A newer
CMake error index, failed configure, or interrupted command cannot confirm old
artifacts. Profile failures do not withdraw independent build trees. Analysis
errors explain unconfirmed/outdated inputs and direct the user to `cmake_configure`.

## External process execution

`ProcessService` is the only module that calls `asyncio.create_subprocess_exec`.
Feature services receive it through constructor injection, select executables, and
construct explicit argument lists. `cmake_run_test` accepts an explicit managed
test executable path; it does not accept external executables or shell commands.
`launch` uses exec mode; interpreters such as
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

Two local operator actions also belong to the process module: `start_detached`
starts a Hub with explicit arguments and disconnected standard streams, and
`open_browser` opens the operator-requested admin URL. They are not MCP execution
tools and do not create development-command transcripts. `ProcessIdentity` uses
psutil's PID and process birth time to distinguish a live runtime from a reused PID.

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
explicitly. `server.py` registers its seven MCP tools and typed Workspace provider.
After toolset discovery, the server initializes analysis and its CMake subscription;
shutdown unsubscribes and closes all sessions before `ProcessService`.
All seven tools bind the packaged `clangd-result.html` widget with the clangd icon.
It renders the original per-configuration answers, including empty results, with
local text filtering, Fields/JSON, and copying. It never reads source files or calls tools.
Diagnostics use severity badges, source ranges, and separate related-location notes.
Hover renders Markdown headings, emphasis, lists, tables, and highlighted C/C++ code
blocks. The bundled markdown-it parser has HTML disabled; link/image rendering is
inert so descriptions cannot navigate or load external content.
Definition/reference locations and workspace-symbol locations additionally carry
`preview: SourceExcerpt(start_line, text) | None`, captured during the analysis call.
The service reads each managed target file once per answer and captures up to seven
lines around the target's start; missing, unreadable, and external files keep their
locations without a preview. These excerpts are part of the original structured
result, so the widget never reads a source file later or observes newer edits.
Navigation widgets group targets by file, with scope headings for workspace symbols,
and switch a highlighted source preview when a saved target is selected. Document
symbols render a collapsible outline preserving clangd's nesting, kinds, signatures,
deprecation tags, and ranges. Closed files and outline branches create their DOM
only when opened. External locations remain visible without bypassing read approval.

CMake retains successful configurations as
`CompilationContext(id, toolset_id, build_directory, compilation_database)`.
It exposes candidates only while their build directory has a confirmed input
baseline and its database exists. Existing artifacts at startup have unknown
freshness and provide no context until a new configure confirms them. Build directories come from profile parameters;
a single native preset can use an explicitly configured build directory (`-B`).
Unknown directories are omitted rather than parsing command output or guessing
CMake preset expansion. Clangd additionally excludes contexts whose toolset lacks clangd.
Explicit analysis tools use their existing `configurations` list to select contexts;
omitting it or passing an empty list selects all available contexts. Workspace
analysis always uses all available contexts. Executables never fall back to another
toolset or a system installation.

`configuration_updates()` is CMake's in-process subscription stream. It yields the
current list immediately and then yields changed snapshots when configure adds or
removes or invalidates a configuration, or build produces a new generation. A slow subscriber keeps only the newest pending snapshot;
it does not delay CMake. A configure result is eligible only after CMake exits successfully, produces a
new successful File API index, supplies readable managed configuration inputs,
and its `compile_commands.json` exists. Publication happens after each
profile execution, not on configure progress messages. Successful configure also
publishes unchanged lists so subscribers can detect changed database contents at the
same paths. Subscribers
close their generators to unregister. Missing CMake tooling produces an empty
initial list rather than blocking server startup.

The typed clangd ToolSpec method `connect` owns `ProcessSession` and JSON-RPC/LSP
framing. Callers exchange typed request, notification, response, and error envelopes.
UTF-8 message bodies are framed by byte length without an added frame-size limit.
The process text transport uses reversible Latin-1 encoding for those bytes; its
transcript is therefore not a decoded UTF-8 LSP log. Clangd uses its normal index
locations, including the cache beside the compilation database. Each process starts
with background indexing and the fixed argument `-j=2`. This worker setting is not
a RAM limit; several configurations still own independent processes and indexes.

`ClangdSession` handles initialization, request correlation, cancellation, document
versions, diagnostics, and shutdown. Progress callbacks reach the session layer.
Positions use one-based lines and zero-based Unicode code-point offsets. The session
requires UTF-32 position negotiation so external locations can be returned without
reading their contents to convert offsets.

Each file operation reads and opens the complete current file with `didOpen`, then
closes it with `didClose` after collecting its result. Diagnostics wait for
`publishDiagnostics` matching that opening's version; unversioned or older
notifications are ignored. LSP requests and semantic-token decoding complete while
the document is open. Cleanup clears its text and diagnostics even after a failed
operation. The session keeps one increasing version counter, with no retained
documents or per-file version map between operations. Repeated requests require a
fresh parse, and `didClose` does not release the process's whole index. Fresh file
diagnostics do not imply that the global background index is fully up to date.

`ClangdService` eagerly starts and retains one session per available context at
initialization and when CMake publishes an available configuration. Its subscription
applies CMake snapshots under the same lock used by analysis. Removed contexts close
their sessions; a changed configuration or database fingerprint replaces only the
affected session. Failed sessions can be recreated by the next operation.
Diagnostics, hover, definition, references, document symbols, and workspace symbols
are implemented in their decorated MCP handlers; shared session and collection
behavior stays on the service.
Each operation has its own result model with `configurations: list[str]`. Equal
complete answers are grouped with their configuration IDs; differing answers remain
separate. An analysis failure raises a domain error rather than silently returning
an incomplete multi-configuration answer. Progress from explicit analysis tools
reaches `ClangdSession`; Workspace provider hooks receive no progress callback.

Clangd's before hooks retain its analysis lock until after or error, preventing
explicit analysis and configuration updates from overlapping a Workspace mutation.
Move captures the source subtree's file paths before it disappears. After writes,
edits, moves, deletes, and directory creation, the provider notifies retained sessions
with `workspace/didChangeWatchedFiles`. There are no documents to reopen between
operations; the next file analysis opens a fresh document against the current saved
headers. Moves notify sessions without producing diagnostic/highlighting result
resources.

After read/write/edit, managed C/C++ files are analyzed across available
configurations. Diagnostics and semantic highlighting are separately grouped and
saved as `ClangdResource` in Workspace under the shared invocation ID. Later edits
never change these resources. Partial reads still analyze the complete file, then
retain only diagnostic ranges and semantic spans intersecting the lines actually
returned by Workspace. Coordinates stay absolute and ranges are not clipped; LSP
range ends remain exclusive. Empty partial-read excerpts, including reads beyond
EOF, have no diagnostic or semantic annotations.
Provider failures close potentially stale sessions and are isolated by Workspace
without changing a successful file operation. Compilation
database changes observed through Workspace also refresh CMake's snapshot and reconcile
sessions. Clangd owns its file reads; it does not watch external edits or manage
editor buffers.

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

The App bridge loads only the linked `diff.json`, `clangd.json`, and
`skipped_files.json` resources, in
parallel, and ignores late responses after cancellation or teardown. Each resource
has its own loading/error state; failures leave the original operation result
visible. The Resources view includes the descriptors and full decoded JSON with
copy controls. It never replaces the original result used by JSON and Copy all.

`clangd-view.js` supplies the analysis projection and standalone diagnostic rendering.
`source-view.js` supplies configuration controls, lexical C/C++ coloring, and
code-point annotations indexed by line in each immutable file. Workspace source/diff
views choose one explicitly displayed configuration for semantic tokens and diagnostics.
Diagnostics appear in selectable hover panels on underlined source ranges, including
source/code and related messages with locations; they are not repeated below the source.
Symbol signatures and documentation require saved clangd hover data, which the
Workspace analysis resource does not currently contain. Diff combines change
spans and local search with annotations on new/context lines only, since clangd
analyzes the resulting file. Move results have a minimal, content-sized view with
only source and destination, without header, controls, footer, or analysis.
Resource reads remain limited to the original
invocation's immutable links.

Fields, Resources, and JSON retain their DOM and scroll positions between tab
switches. File reads draw source lines in short animation-frame batches; a newer
result or teardown cancels pending work. Large JSON (over 100,000 UTF-16 units) uses
one plain-text node instead of individual colored token elements; every value
remains visible and copyable, without truncation. Long lines in that large-JSON
view scroll horizontally instead of wrapping a potentially huge source string.

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
  The `workspace_move` confirmation is an exception: its two rows determine the
  height, capped at 420px for long paths, and there are no header or controls.
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
and verify its generated HTML and icon are packaged. Build with
`npm run build --prefix frontend` or the wheel build. Do not add or run automated
widget tests; the user reviews appearance and behavior visually.

## Git repository operations

`server.py` constructs `GitService` inside the same `ServerRuntime` used by stdio
and the admin SDK client. It injects Workspace, Toolchain, CMake, and clangd;
`--git-toolset` chooses the executable/environment without an implicit fallback.
The Git ToolSpec binds a typed `run` method to ProcessService; only the feature's
explicit handlers construct its argument lists. No arbitrary command MCP surface
or backend interface hierarchy is introduced. Git result models are shared with
the machine-format parsers and plain-text presentation in the feature directory.

Git validates that the workspace equals the worktree root, protects `project/.git`
from Workspace writes, uses literal pathspecs, rejects source paths outside the
project, and removes inherited Git repository/config-injection redirects from the
child environment. Git-owned metadata can reside outside the roots for linked
worktrees; the Git executable accesses it without Python reading those external
files or widening the file tools. Worktree management, init, and clone are outside
the module. Installed Git hooks and operator credential helpers remain enabled.

Read tools return status with separate index/worktree states, raw diff metadata
plus a native patch, stable paginated history, commit details, line porcelain
blame, refs, redacted remote addresses, and conflict index stages. NUL framing
preserves spaces and line breaks in valid UTF-8 filenames. Binary blobs and gitlinks
retain typed identities. Git transport uses reversible Latin-1 internally for
byte-exact machine output, then the feature decodes explicit UTF-8 fields.
Each command bounds retained stdout to 4 MiB and stderr to a short error summary;
the existing process transcript is not copied into an additional operation log.

Mutation tools cover explicit-path staging/unstaging/restore, commits, local branch
management, switch, stash, fetch/pull/push, merge/rebase/cherry-pick/revert, and
continue/abort/skip. Pull defaults to fast-forward only. Push targets one branch;
history replacement requires the expected full remote OID. Stopped operation
detection uses Git's pseudoref resolution, including rebase administrative refs,
without parsing localized status prose or reading external metadata with Python.
Ambiguous pseudoref/branch collisions fail explicitly. Unfinished operations remain
owned by Git across requests and server restarts. Commands are never replayed.

The lock order for mutations is Workspace serialization, clangd's source-change
interval, then CMake's source-change interval. CMake finishes and releases its lock
before clangd reconciles approved contexts. `CMakeProvider` and Git share
`begin_source_change`/`finish_source_change`; unknown changes withdraw uncertain
contexts. Source notifications combine pre/post dirty paths with committed tree
diffs, without a project scan. Failure/cancellation still finalizes dependency
checks; refresh failures cannot conceal a completed commit or push. There is no
Hub-wide execution queue or cross-instance source/build coordination.

Every Git tool binds `assets/git-result.html`, built from `frontend/git-result.html`,
and `icons/git.svg`. It uses the common Apps lifecycle, theme, fixed geometry,
filters, JSON, and original-value copying. Git projections and literal patch/source
rendering live in `frontend/src/git-view.js`; `git.css` contains only the specialized
content layouts and reuses the shared palette, typography, and outer geometry.
Status separates index/worktree states, history emphasizes commit subjects, diffs
group file patches with line gutters and wrapping controls, and blame aligns
attribution with source lines. Conflict stages distinguish missing/binary content;
mutation errors, unfinished operations, and analysis synchronization warnings lead
the result. Native disclosures retain complete labeled fields, including unknown
metadata. The shared custom-render call supplies its existing field renderer,
copying, and tooltip helpers, so Git uses the same clipboard fallback and lifecycle
as ordinary fields. Search is local and reversible, with matched/total counts.
No tool calls, polling, external resources, or additional interactive mutation
controls are present in the widget.
The ordinary admin catalog/history already exposes these tools and their Apps.
The Git monitor reads only the retained last status; it never launches a Git probe.
Retained status/commit text resources and revision completions likewise use saved
data rather than executing resource-time commands. No state is persisted to disk.

Process termination snapshots descendants with psutil. On Windows, managed children
also receive an OS Job Object so MSYS hooks that reparent grandchildren do not
keep pipes open after cancellation. Closing the owned job terminates its remaining
members. If the OS rejects job assignment, ordinary descendant cleanup remains
available. This is process lifecycle management, not a filesystem sandbox.

## Local administration

The CLI starts independent stdio runtimes. Each has its own inherited environment,
toolsets, CMake state, clangd sessions, managed processes, and `MemoryHistory`.
`InstanceAgent` keeps one in-process SDK `Client(mcp, cache=None)` and one outgoing
local IPC connection to a shared `HubService`. The Hub serves the browser and routes
requests; it never constructs feature services or replays their startup environment.

The local IPC endpoint elects one Hub per OS user and logical endpoint name
(`--admin-ipc`, default `forgemcp-hub`). Competing detached Hub candidates exit if
another listener owns that endpoint. Windows uses same-user named pipes with
explicit user permissions and kernel peer checks. Unix uses a private per-user
socket directory, peer identity checks, and exclusive listener ownership. The
shared interface exchanges bytes framed by a four-byte unsigned big-endian length;
`receive()` returns `None` only for clean EOF between frames. Requests and responses
may use the duplex connection concurrently without interleaving frames.

An instance registers its UUID, PID, OS process birth time, roots, startup time,
and version. The Hub checks kernel peer PID when available and checks process
identity using psutil. A temporary connection loss marks that instance disconnected
but retains it while the same process remains alive; PID reuse does not preserve a
dead session. The agent reconnects with bounded backoff and can ask the composition
root to start a replacement Hub. Closing stdio unregisters the runtime. The Hub
allows 15 seconds for initial registration, then stops three seconds after the last
runtime ends. Browser connections and history views do not extend that lifetime.

The panel prefers `127.0.0.1:8765`, falling back to a reserved free port. Its Apps
sandbox uses a second loopback port and browser origin. `--admin-port` affects only
Hub startup; `--no-admin` excludes an individual CLI instance from administration.
`--open-admin` discovers the bootstrap URL through same-user IPC and opens the
browser through the process module. Bootstrap credentials remain in memory and
are never written to operational logs or a token file. The frontend exchanges the
capability for an HttpOnly, same-site session cookie and a CSRF token. HTTP endpoints
check the expected Host, browser Origin/fetch context, and session; POST requests
also require `X-ForgeMCP-Token`. This is a local operator interface, not a remote
network administration service.

### SDK reuse and call history

Hub commands use the existing SDK client methods for catalog listing, tool calls,
resource reads, template listing, and completions. This preserves handler schemas,
validation, real request Context, progress, and SDK elicitation behavior. Direct
`MCPServer.call_tool()` is not used by HTTP handlers: it bypasses middleware and
does not construct a transport request Context. Apps support is advertised by the
in-process client. Concurrent commands reach the same feature service instances;
their existing locks determine execution order.

`MemoryHistory` is SDK middleware for `tools/call`. Each request receives its own
UUID, independently of the JSON-RPC or Hub transport request ID. Records retain
tool name, arguments, source (`stdio` or `admin`), start/finish timestamps, status,
error, reported progress, and the SDK result. Input-required rounds are observable
SDK requests; the middleware does not hold a global execution slot while a client
asks for confirmation. The common progress helper attaches the notifications it
reports to the currently active history record through a ContextVar. The admin
client's direct SDK dispatch carries the source context in the requesting task.

Results stay as SDK data in memory; the bridge serializes standard Pydantic models
with `model_dump(mode="json", by_alias=True, exclude_none=True)` only at the IPC
boundary. Absent optional SDK fields are omitted, as required by MCP and the Apps
handshake schemas; explicit null values inside arguments, JSON schemas, and
structured result dictionaries are preserved. Default invocation IDs, timestamps,
and progress arrays remain present. Original
`content`, `structuredContent`, `isError`, metadata, empty values, and nested fields
remain available to the browser. A successful SDK exchange can still contain a
feature failure, such as a CMake profile's `error`; the structured result remains
the authority for that feature's outcome.

No database, history archive, or result-resource mirror is created. The Hub stores
only live instance registration and pending response routing. History belongs to
the running instance and disappears when it stops. Workspace's existing immutable
result resources and ProcessService's transcripts keep their existing owners;
the admin layer does not archive or copy them into another log.

History, elicitation, and periodic service-state changes send small invalidation
events rather than complete result copies. The browser uses an SSE stream and
fetches snapshots on demand. Monitor state is sampled from retained service data,
without filesystem scans, version probes, or synthetic MCP tool calls. Service
state invalidations occur every two seconds while the instance is connected.
An IPC failure fails outstanding Hub responses; already-started instance calls
are not automatically repeated. Their eventual result remains in instance history.

### HTTP and IPC surface

The main browser entrypoint is `/` (also `/admin.html`). `/api/session` establishes
the authenticated browser session, `/api/events` streams invalidations, and
`/api/instances` lists registered sessions. The instance-scoped endpoints are:

| HTTP route below `/api/instances/{instance_id}` | Behavior |
| --- | --- |
| `GET /snapshot` | Instance identity, tools, static resources, resource templates, history, and service state |
| `GET /history` | Retained tool-call records |
| `GET /calls/{call_id}` | One original invocation and result |
| `POST /calls/{call_id}/cancel` | Request cancellation of the admin client's own running call; returns `{"cancelled":true}` when accepted |
| `GET /state` | Read-only startup settings and retained service monitor data |
| `GET /tools` | SDK tool metadata and input/output schemas |
| `POST /tools/{tool_name}` | Call the tool with `{"arguments": {...}}` |
| `GET /resources` | SDK static resource listing |
| `GET /resource-templates` | SDK resource-template listing |
| `GET /resources?uri=...` | Read the selected resource through the SDK |
| `POST /completion` | Request native SDK completion with MCP `ref`, `argument`, and optional `context.arguments` |
| `GET /elicitations` | Pending questions for this instance's admin client |
| `POST /elicitations/{id}` | Supply an SDK elicitation `{"result": {...}}` |

The bridge uses explicit JSON envelopes on local IPC: registration is
`{"type":"register","instance":{...}}`, acknowledged by
`{"type":"registered","hub_id":"...","url":"..."}`. Commands use
`{"type":"request","id":"...","method":"...","params":{...}}`; responses
echo that transport ID and carry either `result` or `error`. Change notifications
use `{"type":"event","kind":"...","version":N}`. Reconnection does not replay
requests. The `discover` exchange retrieves the private browser URL without
registering a runtime; `unregister` ends an instance's registration.

### Browser behavior and widgets

Each registered runtime has a tab with its own selected history record, catalog,
read-only settings, and monitors. Existing tool schemas drive command controls,
with labeled fields for ordinary input and JSON arguments available for complete
input. Resource-template fields request native MCP completions using the original
template reference and other entered parameters as context. The same bridge accepts
prompt references without inventing a separate suggestion provider. Completion
requests use the authenticated, CSRF-protected `POST /completion` route and never
execute a tool. Command controls are initially
hidden and must be explicitly enabled in the panel; this UI choice does not create
a second authorization or validation policy for the tools.

The selected invocation offers its MCP widget, readable text, and complete raw JSON.
The admin host loads the tool's registered `ui://` HTML and connects it using the
Apps bridge through the separate-origin sandbox proxy. Resource reads made for a
widget include the original `call_id`: the instance allows only an exact URI in
that result's `structuredContent.resources`, under
`forgemcp://workspace/results/`. The original HTML is read separately. Widgets
remain bound to one invocation and cannot use the admin host to execute tools or
browse arbitrary resources. Historical resources are read from their original
in-memory owner, so they are unavailable after the runtime stops.

Cancellation correlates a retained call's admin request ID with the agent's active
SDK task. The cancel endpoint accepts an empty object and returns
`{"cancelled":false}` when that call is no longer active. Requests to cancel a
call owned by stdio are rejected.
Cancelling an admin task uses normal SDK and feature cleanup; it does not create
a second process-termination path or cancel another MCP client's requests.

Elicitation from a web-initiated command is forwarded to the browser as the SDK's
form or URL request. A response completes that admin client's pending question;
unanswered questions cancel after five minutes. Elicitation originating from stdio
continues to belong to the original MCP client and is not answered by the panel.

Vite builds the standalone panel and sandbox proxy into `assets/admin.html` and
`assets/sandbox.html`; the wheel hook packages both alongside the tool widgets.
The Hub returns a clear missing-assets page when those generated files are absent.

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

### Distribution and release verification

The distribution is named `forge-cpp-mcp`; the Python package, server command,
and MCP resource names remain `forgemcp`.

The sdist explicitly includes Python sources, frontend sources, the npm lockfile,
and the wheel build hook. Generated HTML and local npm dependencies are excluded.
`python -m build` builds the wheel from the sdist; installing a wheel requires no Node.js.
Release verification checks metadata, runtime version, and every statically declared
Widget/IconFile asset. CI tests the installed wheel on Windows/Linux and Python
3.13/3.14, and exercises MCP resource availability through an isolated smoke check.
Tag pushes create a draft GitHub release after those checks pass. PyPI publication
is a separate manual workflow using existing release files and Trusted Publishing.
See [releasing.md](releasing.md) for operator commands and account setup.

## Expected next modules

The likely order is:

1. Refactor Workspace result providers around before/after/error notifications and
   sequential Workspace MCP entrypoints. Move tool-specific filesystem work into
   the decorated handlers. CMake already owns its build-tree filesystem work;
   process launches continue through ProcessService.
2. finish migrating backend tests for Workspace providers and clangd lifecycle;
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
