# ForgeMCP architecture

## Status

ForgeMCP is intentionally at foundation stage. The `workspace` feature proves the
module and registration pattern. The shared `process` service adds asynchronous
external-program lifecycle, text transcripts, timeouts, and a read-only inspection
surface before CMake, clangd, quality, and debugger behavior is added.

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
   server process working directory. The server lifespan closes remaining managed
   processes during shutdown.

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
Feature services receive it through constructor injection, select trusted executables,
and construct explicit argument lists. ForgeMCP never invokes a shell and does not
expose a generic command-execution MCP tool.

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
