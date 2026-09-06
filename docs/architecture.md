# ForgeMCP architecture

## Status

ForgeMCP is intentionally at foundation stage. The `workspace` feature proves the
module and registration pattern. The shared `process` service adds asynchronous
external-program lifecycle, text transcripts, timeouts, and a read-only inspection
surface before CMake, clangd, quality, and debugger behavior is added.
The `toolchain` service discovers independent toolsets once at startup and exposes
their paths and versions through a cache and read-only MCP surface.

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
    service.py                  # immutable cache, Python API, MCP registration
    discovery.py                # provider orchestration and version probes
    loader.py                   # deterministic pkgutil built-in enumeration
    spec.py                     # toolsets, specs, commands, async parser execution
    errors.py                   # expected configuration and command errors
    providers/                  # system PATH, explicit CLI, Visual Studio layouts
    tools/                      # one unbound SPEC per built-in module
tests/
  conftest.py                   # isolated copy of the C++ acceptance workspace
  <feature>/test_service.py     # direct business behavior
  test_server.py                # MCP surface and protocol behavior
frontend/
  src/                          # widget JavaScript and CSS
  *.html                        # Vite entry points
examples/cpp-acceptance-project # portable CMake fixture
```

Create extra feature files only around a concrete responsibility. For example, a
CMake module may grow `output_parser.py` and `kits.py`; it should not start with
interfaces and factories for hypothetical parsers or kit providers.

## Composition and dependency injection

`src/forgemcp/server.py` is the only composition root:

1. Resolve operator configuration such as the workspace root.
2. Construct the long-lived `ProcessService`, then feature services, passing shared
   dependencies explicitly.
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

### `workspace_overview` tool

The tool scans one configured workspace without modifying it and returns a
`WorkspaceOverview` Pydantic model. The MCP Python SDK derives the output schema,
validates the result, serializes JSON text into `content`, and sends the same object as
`structuredContent` for the widget. The scan:

- recognizes common C and C++ source/header extensions;
- skips common VCS, virtual-environment, cache, build, and IDE directories;
- does not follow directory symlinks;
- is capped at 10,000 visited files; and
- reports whether the result was truncated.

Its three progress notifications are monotonically increasing. They are safe to emit
unconditionally because the SDK makes them a no-op when the caller did not request
progress.

### Workspace files resource

`forgemcp://workspace/files/{extension}` returns bounded JSON containing at most 500
sorted, workspace-relative paths. Supported extensions are offered through MCP
completion. An unknown extension becomes `ResourceNotFoundError`, not an unhandled
exception.

### Inspection prompt

`inspect_cpp_workspace(focus)` seeds a read-only inspection flow. Completion suggests
the current focus vocabulary. Prompt arguments and resource-template parameters are
the only MCP primitives that support completion; static resources do not.

## External process execution

`ProcessService` is the only module that calls `asyncio.create_subprocess_exec`.
Feature services receive it through constructor injection, select executables, and
construct explicit argument lists. Exec mode remains the default. Optional
`shell=True` formats the executable and arguments for the native shell; process
records retain the original executable, argument list, and shell flag. ForgeMCP does
not expose a generic command-execution MCP tool.

`start`, `start_protocol`, and `launch` support `inherit_environment=True`, which
copies the server environment then overlays `env`. With `False`, only `env` is passed
to the child. No environment values are added to operational lifecycle logs.

The process module provides two concrete consumption modes:

- `start(...) -> ProcessSession` exposes one `output()` async iterator. Permanent
  stdout and stderr reader tasks decode both pipes concurrently and publish tagged
  `ProcessOutput(stream, text)` chunks through one bounded queue. The iterator ends
  after both streams reach EOF. Cross-stream ordering is the order ForgeMCP observed,
  not an operating-system ordering guarantee.
- `start_protocol(...) -> ProtocolProcessSession` exposes separate `read_stdout()`
  and `read_stderr()` operations, with one active reader allowed per pipe. Text is the
  default. `raw_stdout=True` is reserved for byte-counted framing such as clangd LSP;
  those bytes are still decoded separately for the retained text transcript.

Each pipe owns an incremental decoder so a multibyte character split across OS reads
is reconstructed correctly. The default encoding is `locale.getencoding()` and each
consumer may select an explicit encoding. Replacement decoding is the resilient
default for human-facing tool output; strict decoding is available for formal
protocols. stdin accepts text in ordinary sessions and additionally accepts bytes in
raw protocol sessions.

Every stdin write and stdout/stderr read is appended to a process transcript in
memory. The record also retains identifiers, command metadata, lifecycle timestamps,
encoding, timeout policy, state, and return code. This is inspection state, not an
operational log: stderr logging contains only process identifiers and lifecycle
summaries and never copies transcript content or environments.

Timeout configuration has three effective forms:

- `timeout=None`: disabled;
- `timeout=<seconds>, timeout_mode="total"`: measured from process start;
- `timeout=<seconds>, timeout_mode="idle"`: reset whenever stdout or stderr produces
  a non-empty byte chunk.

On timeout or explicit termination, the directly managed asyncio process receives
`terminate()`, followed by `kill()` if it remains alive after a short grace period.
Task cancellation performs the same cleanup before propagating cancellation. Process
groups and descendant-tree management are intentionally outside the current scope.

The read-only MCP surface consists of `processes_overview(status)`, its process-list
widget, and `forgemcp://processes/{process_id}`. Completion suggests retained process
IDs. The resource exposes detailed state and the complete text transcript; it cannot
start or stop a process. A well-formed URI whose process ID is not retained raises
`ResourceNotFoundError`; other unexpected exceptions remain unwrapped so the SDK
sanitizes them as resource crashes.

## Toolset discovery and command execution

`ProcessService -> ToolchainService` is wired explicitly in `server.py`. The lifespan
awaits `ToolchainService.initialize()` before requests; an initialization lock prevents
duplicate discovery. A complete sorted tuple is published atomically and retained
for the server lifetime. There is no automatic refresh, watcher, global selection,
service locator, or plugin framework.

`loader.py` enumerates `forgemcp.toolchain.tools` with `pkgutil.iter_modules`, skips
private names, sorts module names, and requires exactly one valid unbound `SPEC` per
module. Duplicate logical names and invalid modules raise domain errors. Adding a
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
PATH on configuration errors. All version commands run through bound specs; failed
or unrecognized versions remain null without removing found executables.

`Toolset` and `ToolSpec` are frozen dataclasses with read-only mapping snapshots.
Toolsets retain only their own bound specs and environment. The public Pydantic
summaries/details are fresh views that omit environment and expose absolute paths.
`list_toolsets()` and `get_toolset(id)` serve MCP; future feature services use
`resolve_toolset(id)` and `get_tool(id, name)` with an explicitly supplied toolset ID.
There are no `selected`, `current`, or `preferred` fields.

`ToolCommand` separates a synchronous keyword argument builder from execution. Binding
creates a new command with the executable, ProcessService, parser, and toolset
environment. Commands and parsers use arbitrary string keys. Built-ins currently
provide version probes; `cppvsdbg` deliberately has none. Parser helpers remain
platform-independent and consume tagged stdout/stderr incrementally.

`CommandExecution` launches lazily once, supports both `await` and `async for`, and
retains `parsed_result` plus the terminal `process_result`. Its single event consumer
receives live parser events through a bounded queue. Await-only execution discards
events; events are not replayed as a second transcript. After streaming,
`await execution.result()` returns the same cached result. Early parser completion
still drains both pipes, and successful parsing never hides process failure. Parser
exceptions name the command without copying output into errors. Cancellation and
early closure of the event iterator terminate the managed process.

`toolsets_list` returns summaries (the SDK wraps a list in `structuredContent.result`),
and `toolset_get` returns one details object. Both report progress and carry read-only
annotations, icons, Apps metadata, and SDK text fallback. The toolsets widget requests
cached details when the user chooses an item; this UI choice never sets server-wide
state. The static list resource and details template return markdown, and completion
offers cached toolset IDs. Unknown IDs become ToolError or ResourceNotFoundError at
the corresponding boundary; unexpected exceptions remain SDK-sanitized.

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

1. workspace-safe read/write operations and path policy;
2. CMake discovery/configure/build/test using `ProcessService`;
3. clangd lifecycle and language operations;
4. formatting, static analysis, and sanitizer parsing;
5. debugger adapter lifecycle and DAP operations;
6. persistent process transcripts and configurable retention limits.

This ordering is guidance, not a framework contract. Add the smallest end-to-end slice
needed by the next user-visible workflow.
