# ForgeMCP

ForgeMCP is a Python MCP server for structured C and C++ development workflows.
The repository contains workspace, process, toolchain, and initial CMake services.
Clangd tools provide read-only analysis across CMake configurations; their widgets
remain deferred. External file reads ask for confirmation through MCP elicitation. Quality and
debugger modules are planned.

## Current MCP surface

- Workspace tools browse directory trees, find files, read UTF-8 text and file
  metadata, search literal text or regex, write/edit/move/delete files, and create
  directories. Each tool reports progress and has a packaged MCP App and icon.
- Workspace resources mirror UTF-8 text and raw bytes, and expose directory trees,
  file lists, metadata, and search results as Markdown. Resource parameters have
  completions for qualified paths, extensions, depth, and boolean options.
- Clangd tools list available configurations and provide diagnostics, hover,
  definitions, references, document symbols, and workspace symbols. Empty
  configuration selections use all available contexts.
- No prompts are currently registered.
- CMake tools list operator profiles, configure projects, build targets, and run
  CTest. Each tool has its own widget with profile filters, Fields/JSON views,
  copying and per-test results. Command logs are available through `process_get`. CMake syntax highlighting remains deferred.
- Tool `processes_overview` shows running and completed external development tools,
  including exit codes and timeout interruptions. `process_get(process_id)` returns
  one process with its ordered stdin/stdout/stderr transcript.
- Resources `forgemcp://processes` and `forgemcp://processes/{process_id}` expose
  Markdown snapshots of the list and one process.
- Tool `toolsets_list` lists every discovered toolset; `toolset_get(toolset_id)` returns
  its absolute executable paths, kinds, and versions. Both open the toolsets widget.
- Resources `forgemcp://toolsets` and `forgemcp://toolsets/{toolset_id}` expose the
  toolsets as markdown, querying available versions for details, with completion
  for retained toolset IDs.

The tools remain useful in clients without MCP Apps support because the Python SDK
serializes their typed results into both text `content` and `structuredContent`.

All widgets share a compact console style with a fixed 420px height and adapt to the
host width. Long values wrap and remain selectable; overflow scrolls vertically.
File views separate line numbers and offer a wrap toggle with horizontal scrolling.
Search groups matching lines by collapsible file, highlights matches, expands clipped
context on click, and keeps skipped files in a separate tab.
Fields, local filters, copy icons and syntax-highlighted JSON show the supplied result
without making additional tool calls. Process times are readable to seconds; JSON and
copying preserve the original precision. A toolsets list shows summaries, while a
`toolset_get` result shows the details returned by that call.

ForgeMCP does not expose arbitrary command execution over MCP. Feature services use
the shared process service internally; its public MCP surface is read-only.

## Requirements

- Python 3.13 or newer
- Node.js `^20.19.0` or `>=22.12.0` for clean editable installs, wheel builds,
  and widget development

The server is built against MCP Python SDK `2.1.1`.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Build

One command installs the locked frontend dependencies, builds every widget, and
creates the Python wheel with its HTML and icon assets:

```powershell
.\.venv\Scripts\python.exe -m build --wheel
```

The wheel is written to `dist/`. Generated HTML under `src/forgemcp/assets/` is build
output; change its source under `frontend/`, never the HTML directly.

## Run

Run against the current directory:

```powershell
.\.venv\Scripts\forgemcp.exe
```

Pass `--workspace` only when the target differs from the server process working
directory:

```powershell
.\.venv\Scripts\forgemcp.exe --workspace examples/cpp-acceptance-project
```

In VS Code, the `ForgeMCP: server` launch configuration is the one-button path: it
builds the wheel first and then starts the server for this repository.

All MCP traffic uses stdout. Operational logs must go to stderr.

## Workspace files and storage

All file tools use string paths such as `project/src/main.cpp` and
`storage/build/debug`. Directory tools default to `project/`; the separate tool
argument `root` has been removed. Python consumers use the shared `WorkspacePath`
type, which serializes as the same string. Storage defaults to `.<project-name>.forgemcp` beside the
project. Set its location explicitly when needed:

```powershell
forgemcp --workspace C:\Projects\Example --workspace-storage D:\ForgeMCP\Example
```

Progress notifications in workspace, process, and toolchain are limited to one per second per invocation by
default. Configure this with `--progress-interval 2.5` (seconds), or use
`0` to disable throttling. The first notification is immediate; intervening updates
are dropped, not queued. Tool results signal completion even when its progress
notification is suppressed. The interval must be finite and nonnegative.

Storage is created on first use. Its persistent directories survive restarts;
internal temporary-directory contexts clean up their own folders on exit. The
process service permits working directories inside either configured root. This is
a working-directory check, not an operating-system sandbox.

| Tool | Operation |
| --- | --- |
| `workspace_list(path="project/", depth=1, include_hidden=false)` | Directory tree; `depth=null` expands the whole tree; a file path is an error |
| `workspace_find_files(pattern="*", path="project/")` | Recursive filename/path glob search |
| `workspace_file_info(path)` | Creation/modification times, byte size, owner; unavailable metadata is null |
| `workspace_read_file(path, start_line=1, end_line=null)` | UTF-8 text, with an optional inclusive line range |
| `workspace_search(query, path="project/", regex=false, extensions=null, case_sensitive=true)` | Matching lines and skipped binary/non-UTF-8 files |
| `workspace_write_file(path, text)` | Create or overwrite; return removed/added line counts |
| `workspace_edit_file(path, old_text, new_text, replace_all=false)` | Exact replacement; zero or ambiguous matches fail without modifying the file |
| `workspace_move(source, destination)` | Move a file or directory; destination must not exist |
| `workspace_delete(path)` | Delete a file or empty directory |
| `workspace_mkdir(path)` | Create a directory and missing parents |

Text is UTF-8; writes use a sibling temporary file and `os.replace`. There are no
revision/hash parameters. Edits preserve text outside the replacement, including
line endings. Parents must already exist for file writes and moves.

Directory trees hide dot-prefixed directories unless `include_hidden=true`;
dot-prefixed files remain visible. Searches skip directories beginning with a dot and do not follow links. Ordinary
`build` directories and dot-prefixed files are searchable. Explicit file reads may
access hidden directories and in-root links. Mutations cannot traverse links;
moving/removing a whole tree containing links is rejected. Dependent modules may
register protected paths, which remain readable but cannot be changed through the
workspace API. Searches intentionally have no application-level timeout, result
limit, or pagination in this iteration.

File resource templates use a single path including its project/ or storage/ prefix:

```text
forgemcp://workspace/file{/path*}
forgemcp://workspace/raw{/path*}
forgemcp://workspace/list{?path,depth,include_hidden}
forgemcp://workspace/find-files{?pattern,path}
forgemcp://workspace/file-info{?path}
forgemcp://workspace/search{?query,path,regex,extensions,case_sensitive}
forgemcp://workspace/results/{result_id}/{name}.json
forgemcp://workspace/results/{result_id}/{name}.md
```

Examples: `forgemcp://workspace/file/project/src/main.cpp`,
`forgemcp://workspace/raw/storage/build/debug/app.exe`, and
`forgemcp://workspace/search?path=project/&query=TODO&extensions=cpp,hpp`.
Use `depth=all` for a full resource tree and `include_hidden=true` to include
dot-prefixed directories. Text-search extensions are comma-separated;
an omitted parameter means any extension and `extensions=` means extensionless
files. Encode query values using percent encoding (spaces as `%20`, not `+`);
regex is supported. Text mirrors return the original text, raw mirrors return MCP
binary content, and the other four resources return `text/markdown`.

Dependent modules may register workspace result providers. A tool's `resources`
object maps each contributing provider name to a descriptor with `uri` and
`mime_type`, plus optional provider-specific JSON metadata. There is no manifest
or `extensions_uri`. All result resources are immutable, held in memory, and
disappear on restart.

Write/edit return `resources.diff` linking to `diff.json` with `version: 1`.
This typed resource contains `path`, compact `changes` ranges, and `hunks` with
context/added/removed lines, original/new numbers, exact text including line
endings, and character `spans` in zero-based Unicode code points [start, end).
Starts are one-based; a zero count denotes an empty range at an insertion position.
Diff data is absent from the primary result. Widgets read the named resource
and display changes without headers or a redundant diff label. The Highlight
changes button toggles changed-substring highlighting next to Wrap lines; local
search uses a separate color. JSON and Copy all retain the original tool result.
A resource-loading failure does not change the successful file-operation outcome.
No syntax/diagnostic provider is included yet.

## CMake profiles

`cmake_profiles` lists profiles. `cmake_configure`, `cmake_build`, and `cmake_test`
operate on all profiles unless a nonempty `profiles` list selects a subset.
Configuration must precede building, and building must precede testing. Profiles
run sequentially; one failed execution does not suppress later executions.
Each tool returns its own list of per-profile/preset results. The sole outcome
field is `error`: null means success, otherwise it explains the failure (including
the exit code for a failed command). Results include `process_id` for reading
command output through `process_get`; they do not duplicate logs. Configure returns the build-directory and compilation-database paths
when known; build returns parsed step counts when available; test returns JUnit cases.
Full process transcripts remain in the process module; CMake results contain no
transcript links. The default command timeout is 600 seconds per execution and
can be overridden with the existing `ProcessTimeout` shape (`total`, `idle`).

With no explicit profiles:

- If `CMakePresets.json` or `CMakeUserPresets.json` exists, the automatic `presets`
  profile runs all available configure, build, or test presets for that operation.
  Names come from the selected CMake's `--list-presets=all`; ForgeMCP does not parse
  preset JSON, inheritance, conditions, macros, or associations.
- Otherwise, `Debug` and `Release` profiles use separate
  `storage/build/cmake-Debug` and `storage/build/cmake-Release` directories.

`--cmake-toolset ID_OR_NAME` selects the automatic profiles' toolset (default:
`system`). Repeated `--cmake-profile NAME KEY=VALUE ...` replaces automatic profiles:

```powershell
forgemcp --workspace C:\Projects\Example `
  --cmake-profile debug toolset=system configuration=Debug generator=Ninja `
  --cmake-profile release toolset=system configuration=Release generator=Ninja
```

Profile settings are `toolset`, `configuration`, `generator`, `build-directory`,
`c-compiler`, `cxx-compiler`, `toolchain-file`, and repeatable
`define=CMAKE_VARIABLE=value`. Compiler names refer to tools in the selected
toolset. Build directories and toolchain files use `project/...` or `storage/...`.
Plain profiles default to the Ninja generator and enable compilation-database
export. Ninja must exist in the selected toolset; there is no generator fallback.
Without a compiler setting, CMake discovers the compiler in the toolset's
environment. ForgeMCP does not select another toolset as a fallback.
When the generator changes, configure completely removes the existing build
directory and recreates it before configuring. The cache must belong to this
project, and workspace root, protected-path, and symlink checks still apply.

Native preset profiles use repeatable `configure-preset`, `build-preset`, and
`test-preset` settings; ForgeMCP does not infer links between them:

```powershell
forgemcp --workspace C:\Projects\Example `
  --cmake-profile native toolset=system `
    configure-preset=ninja-debug `
    build-preset=build-ninja-debug `
    test-preset=test-ninja-debug
```

Preset execution delegates directories, toolchains, and environments to CMake/CTest;
ForgeMCP neither resolves nor prevalidates `binaryDir`. A profile may supply
`build-directory` for ordinary build/test commands when corresponding presets are
absent. This must identify the preset's actual build directory inside project/storage.
Such ordinary commands use the toolset environment, not a replay of configure-preset
environment variables; use build/test presets when their inherited environment is needed.
Preset configure settings cannot be mixed with manual generator/compiler/cache settings.
No profile settings are read from environment variables or persisted by ForgeMCP.
Progress messages identify the profile and current configure step, build action,
or CTest case, with the common notification throttle. Preset generators are not
overridden; their compilation-database settings remain controlled by the preset.

The initial CMake slice does not yet include clean/project-inspection tools
or syntax highlighting. Unit and in-process MCP tests cover profiles,
command parsing, generator changes, result resources, and qualified paths.
Existing workspace widgets have not yet been adapted or validated against the new path contract.

## Toolchain discovery

Before serving requests, ForgeMCP discovers one System toolset from `PATH` and, on
Windows, a separate toolset for every Visual Studio instance reported by the standard
Installer `vswhere.exe`. Each VS toolset uses its own `VsDevCmd.bat` environment.
Toolsets can be incomplete or empty. ForgeMCP does not select a current or preferred
toolset; consumers must supply an explicit ID.

Add separate user toolsets with repeated `--toolset NAME TOOL=PATH ...` options:

```powershell
forgemcp `
  --toolset "LLVM 20" `
    "cmake=C:\Tools\CMake\bin\cmake.exe" `
    "clang=C:\LLVM20\bin\clang.exe" `
    "clang++=C:\LLVM20\bin\clang++.exe" `
    "clangd=C:\LLVM20\bin\clangd.exe" `
  --toolset "LLVM 22" `
    "clang++=D:\LLVM22\bin\clang++.exe" `
    "lldb-dap=D:\LLVM22\bin\lldb-dap.exe"
```

Names must be unique, and tool names must match built-in specs. Relative paths resolve
against the server working directory; paths inside the workspace are permitted.
Invalid explicit paths are configuration errors and never fall back to system tools.
User toolsets inherit the server environment. Their IDs are deterministic hashes of
their names, so changing paths keeps an ID stable.

Built-ins cover CMake, CTest, Ninja, Make, MSBuild, MSVC (`cl`), `link`, Clang,
`clang++`, `clang-cl`, GCC, `g++`, LLD, `lld-link`, clangd, Git, LLDB-DAP, GDB, and
`cppvsdbg`. The last adapter may be found as `OpenDebugAD7.exe` or VS Code's bundled
`vsdbg.exe`; it has no portable version probe. An unknown or failed version probe
leaves the discovered path available with a null version. Version methods for `cl`,
`link`, `cppvsdbg`, and `lldb-dap` are not implemented in this iteration.

Toolsets are discovered once per server lifetime; restart to discover changed
installations. Detail tools and resources query available versions on each request,
without caching them. Listing toolsets does not launch version probes. All probes,
`vswhere`, and the command capturing `VsDevCmd` plus `set` remain visible in the process
overview and retain their transcripts. Toolset resources omit environments; the
environment capture's stdout is retained in its ordinary process transcript.

## Tests

Tests that need a C/C++ workspace use the shared `cpp_acceptance_project` fixture from
`tests/conftest.py`. It copies the complete acceptance project into a fresh pytest
temporary directory for each test, so tests can modify their workspace without
changing repository files.

## Verify

```powershell
npm test --prefix frontend
.\.venv\Scripts\python.exe -m build --wheel
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

See [docs/architecture.md](docs/architecture.md) for module boundaries and the
registration lifecycle. Repository rules for coding agents live in [AGENTS.md](AGENTS.md).

## Reading process logs

`process_get` accepts `lines`, `time`, and `max_bytes` (default 65536). With neither
selector it reads the last 100 lines. For example:

```json
{"process_id": 42, "time": {"last": 1}, "lines": {"last": 100}, "max_bytes": 4000}
```

Each selector takes exactly one form: `{"first": N}`, `{"last": N}`, or
`{"start": A, "end": B}`. Lines start at 1 and ranges include both endpoints.
Time uses seconds from process start and includes start but excludes end. Last
seconds are relative to request time while running and completion after exit.
Time is measured when a chunk is recorded, not when each character was produced.

Selection applies time, then lines within that selection, then the combined UTF-8
text byte budget (excluding JSON/metadata). Absolute line numbers are retained.
Tail selections keep the end when bytes run out; other selections keep the start.
A lines selector determines direction when supplied, otherwise the time selector does.
Partial lines are allowed, but UTF-8 characters are never split. `max_bytes=0`
returns metadata without text. The response adds only `lines: [first, last]`, or
null for an empty selection; each entry carries its own inclusive start/end lines.
All streams share line numbering in observed order; entries can overlap a line.
LF advances the line, including split CRLF; standalone CR is preserved as text.
The retained journal and the existing process Markdown resource remain complete.
