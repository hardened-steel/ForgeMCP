# ForgeMCP

ForgeMCP is a Python MCP server for structured C and C++ development workflows.
The repository contains workspace, process, toolchain, and initial CMake services.
Clangd tools provide read-only analysis across CMake configurations with a shared
result widget. External file reads ask for confirmation through MCP elicitation. Quality and
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
- Git tools inspect and modify the repository rooted at the workspace: status,
  diffs, history, blame, branches/tags/remotes, staging, commits, restore, stash,
  fetch/pull/push, merge/rebase/cherry-pick/revert, and conflict stages and continuation.
  Every tool has typed data, plain text, a shared Git App, and process provenance.
- CMake tools list operator profiles, configure projects, build targets, and run
  CTest. `cmake_run_test` directly runs a project/storage test executable with
  literal command-line arguments. Each tool has its own widget with Fields/JSON views,
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

Every tool returns readable English plain text in `content` alongside its typed
`structuredContent`. Trees use branch characters, source excerpts use line numbers,
and diagnostics use compiler-style messages. Text does not include Markdown
formatting or Workspace provider resource links; the structured data retains those
links for widgets. Clients decide how to display the text and may prefer structured
data when supplying results to a model.

All widgets share a compact console style with a fixed 420px height and adapt to the
host width. The move confirmation uses only two rows, source and destination.
Long values wrap and remain selectable; overflow scrolls vertically.
File views separate line numbers and offer a wrap toggle with horizontal scrolling.
Search groups matching lines by collapsible file, highlights matches, expands clipped
context on click, and keeps skipped files in a separate tab.
Fields, local filters, copy icons and syntax-highlighted JSON show the supplied result
without making additional tool calls. Process times are readable to seconds; JSON and
copying preserve the original precision. A toolsets list shows summaries, while a
`toolset_get` result shows the details returned by that call.

ForgeMCP can directly run an explicitly selected test executable inside the
project or storage root; it does not expose shell commands or external executable
paths over MCP. Feature services use
the shared process service internally; its public MCP surface is read-only.

## Requirements

- Python 3.13 or newer
- Node.js `^20.19.0` or `>=22.12.0` for source builds, clean editable installs,
  and widget development; it is not required to install or run the release wheel

The server is built against MCP Python SDK `2.1.1`.

## Install a release

Download the `.whl` file from [GitHub Releases](https://github.com/hardened-steel/ForgeMCP/releases)
and install it in a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .\forge_cpp_mcp-0.4.0-py3-none-any.whl
.\.venv\Scripts\forgemcp.exe --help
```

The wheel includes the compiled HTML widgets and icons. No npm commands or separate
frontend deployment are needed. It also packages the local admin panel and its
isolated widget host. CMake, Ninja, compilers, and clangd are external
tools: install the ones needed by your workflows separately.
The distribution name is `forge-cpp-mcp`; the Python module and server command are `forgemcp`.

After the package is published to PyPI, the install command can instead be:

```powershell
.\.venv\Scripts\python.exe -m pip install forge-cpp-mcp==0.4.0
```

## Setup from source

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

The wheel is written to `dist/`. To build both the source distribution and a wheel
from that source distribution, use `python -m build` in the development environment.
Generated HTML under `src/forgemcp/assets/` is build
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

## Local admin panel

Normal CLI launches automatically join one admin Hub for the current OS user.
Each server remains an independent stdio process with its own workspace,
environment, toolsets, processes, and clangd sessions. The Hub presents them as
separate browser tabs; it does not move their services into a shared process.

Open the panel while at least one server is running:

```powershell
forgemcp --open-admin
```

This command retrieves the private browser URL through same-user local IPC and
opens the system browser, then exits. The startup message contains the public
address and this command; the authentication token is never printed. Opening the
public address in a new browser session alone does not authenticate that session.

Each instance tab offers:

- tool-call history from both MCP clients and the browser, with original arguments,
  results, progress, timestamps, and errors;
- the original MCP App widget, text fallback, and complete JSON result;
- tool schemas, static resources, resource templates, and resource reading;
- startup settings, process status, discovered toolsets, CMake configurations,
  and retained clangd sessions;
- browser responses to SDK elicitation requests, including external-file read
  confirmation for commands initiated from the panel.

Settings are read-only. Command controls start hidden; select **Enable command
controls** to call an existing tool using fields generated from its input schema.
JSON input remains available for complete arguments. Resource templates provide
parameter fields and native MCP completion suggestions, using other entered
parameters as context. These
calls pass through the same SDK handlers, validation, and feature locks as stdio
calls. There is no panel-wide execution queue. Existing modules decide which
operations may run concurrently.
The panel can cancel its own running commands; calls initiated by an MCP client
remain owned by that client.

The panel listens on `127.0.0.1`, preferring port 8765 and choosing a free port when
that port is unavailable. Widgets use a second loopback port for their isolated
host. The Hub stops shortly after the last server session ends; browser tabs do
not keep it running. A temporary IPC disconnect is shown as reconnecting while
the underlying server process remains alive. Lost tool calls are never replayed
automatically; read the instance history after reconnecting before repeating one.

History and linked widget result resources live only in the server's memory.
Stopping that server loses its history; there is no database or disk archive.
Process transcripts retain their existing in-memory lifecycle and are not copied
into a separate admin log.

Use these operator options when needed:

```powershell
forgemcp --no-admin
forgemcp --admin-port 9000
forgemcp --admin-ipc forgemcp-other
forgemcp --open-admin --admin-ipc forgemcp-other
```

`--no-admin` opts that instance out of the panel. `--admin-port` is a preference
when starting a new Hub; it does not change an already running Hub's port.
Instances using the same `--admin-ipc` name share a Hub; a different name creates
a separate group for the same OS user.

The normal wheel build includes `admin.html` and `sandbox.html` alongside the MCP
widgets. After changing frontend source, use the documented wheel build or
`npm run build --prefix frontend`. An unbuilt source checkout returns an explicit
missing-assets page until those files are generated.

## Connect Codex or Claude Code

After [installation](#install-a-release) or [source setup](#setup-from-source), copy the appropriate example into the root of the C/C++
project you want ForgeMCP to work on:

| Client | Example | Destination |
| --- | --- | --- |
| Codex | [`examples/mcp-clients/codex/config.toml`](examples/mcp-clients/codex/config.toml) | `.codex/config.toml` |
| Claude Code | [`examples/mcp-clients/claude-code/.mcp.json`](examples/mcp-clients/claude-code/.mcp.json) | `.mcp.json` |

In the copied file, replace both example paths with absolute paths: the first points
to the `forgemcp.exe` installed by setup, and the `--workspace` argument points to
the C/C++ project root. The examples use Windows paths with forward slashes, which
work in both TOML and JSON. On macOS or Linux, point `command` to the virtual
environment's `bin/forgemcp` instead. If either destination file already exists,
add the `forgemcp` entry to it rather than replacing the file. Restart the client
after editing its configuration. Check the connection with `codex mcp list` or
`claude mcp get forgemcp`, respectively.

To require agents to use ForgeMCP for supported operations, append the
[example agent instructions](examples/mcp-clients/agent-instructions.md) to your
project's `AGENTS.md` (Codex) or `CLAUDE.md` (Claude Code). The same block works
for both clients and requires evidence of an unsupported operation, tool failure,
or unavailability before using alternatives. Preserve existing project instructions.

## Workspace files and storage

All file tools use string paths such as `project/src/main.cpp` and
`storage/build/debug`. Directory tools default to `project/`; the separate tool
argument `root` has been removed. Python consumers use the shared `WorkspacePath`
type, which serializes as the same string. Tool path arguments also accept ordinary
relative paths: `src/main.cpp` and `./src/main.cpp` both mean `project/src/main.cpp`,
and `./` means `project/`. One trailing `/` is accepted for managed paths:
`project/src/` becomes `project/src`. Results always use canonical qualified paths.
The prefixes `project/`, `storage/`, and `root/` are reserved; use `./storage/...`
to access a project directory named `storage`. Absolute paths, `..`, and repeated
separators remain invalid; external locations require an explicit `root/` prefix.

Storage defaults to `.<project-name>.forgemcp` beside the project. Set its location
explicitly when needed:

```powershell
forgemcp --workspace C:\Projects\Example --workspace-storage D:\ForgeMCP\Example
```

Progress notifications in workspace, process, and toolchain are limited to one per second per invocation by
default. Configure this with `--progress-interval 2.5` (seconds), or use
`0` to disable throttling. The first notification is immediate; intervening updates
are dropped, not queued. Tool results signal completion even when its progress
notification is suppressed. The interval must be finite and nonnegative.

Storage is created on first use. Its persistent directories survive restarts;
each feature module manages its own directories and cleanup. The
process service permits working directories inside either configured root. This is
a working-directory check, not an operating-system sandbox.

| Tool | Operation |
| --- | --- |
| `workspace_list(path="project/", depth=1, include_hidden=false)` | Directory tree; `depth=null` expands the whole tree; a file path is an error |
| `workspace_find_files(pattern="*", path="project/")` | Recursive filename/path glob search |
| `workspace_file_info(path)` | Creation/modification times, byte size, owner; unavailable metadata is null |
| `workspace_read_file(path, start_line=1, end_line=null)` | UTF-8 text, with an optional inclusive line range |
| `workspace_text_search(query, path="project/", regex=false, extensions=null, case_sensitive=true, max_matches=100)` | Bounded matching lines, exact counts, and a linked skipped-file list |
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
workspace API. Searches have no application-level timeout or pagination. Text search
returns at most `max_matches` matching lines (positive integer, default 100), with
all spans on each returned line. It continues scanning to return exact
`matches_count` and `skipped_files_count`; `matches_truncated` indicates omitted
matching lines. The complete binary/non-UTF-8 file list is an immutable JSON resource
linked as `resources.skipped_files` when nonempty, rather than inline output.
The widget loads that snapshot for its skipped-files tab. Narrow the path/extensions
or increase `max_matches` to retrieve more matching lines. Individual lines and their
span arrays have no byte limit.

File resource templates use a single path including its project/ or storage/ prefix:

```text
forgemcp://workspace/file{/path*}
forgemcp://workspace/raw{/path*}
forgemcp://workspace/list{?path,depth,include_hidden}
forgemcp://workspace/find-files{?pattern,path}
forgemcp://workspace/file-info{?path}
forgemcp://workspace/search{?query,path,regex,extensions,case_sensitive,max_matches}
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
`mime_type`. There is no manifest
or `extensions_uri`. All result resources are immutable, held in memory, and
disappear on restart.

Write/edit return `resources.diff` linking to `diff.json` with URI and MIME type.
This typed resource contains `path`, compact `changes` ranges, and `hunks` with
context/added/removed lines, original/new numbers, exact text including line
endings, and character `spans` in zero-based Unicode code points [start, end).
Starts are one-based; a zero count denotes an empty range at an insertion position.
Diff data is absent from the primary result. Widgets read the named resource
and display changes without headers or a redundant diff label. The Highlight
changes button toggles changed-substring highlighting next to Wrap lines; local
search uses a separate color. JSON and Copy all retain the original tool result.
A resource-loading failure does not change the successful file-operation outcome.
When a CMake configuration has a compilation database and clangd in its toolset,
Workspace reads, writes, and edits of C/C++ files also return
`resources.clangd`. This immutable JSON contains diagnostics and semantic
highlighting grouped by configuration, always across all available configurations.
Clangd analyzes the complete file even for a partial Workspace read, but the saved
diagnostics and semantic spans include only those intersecting the lines actually
returned. Their coordinates remain absolute file positions; an empty partial-read
excerpt (for example, beyond EOF) has no annotations.

Clangd starts sessions from CMake's configuration subscription and keeps them until
the configuration disappears, its database changes, or the server stops.
Each process receives `-j=2`; this worker setting does not impose a memory limit.
File analysis opens the complete current file, collects the requested result, and
closes the document before returning. Documents are not retained between calls, so
the next analysis incorporates saved header changes without reopening other files
after each Workspace mutation. Repeated calls pay the cost of a fresh file parse;
closing a document does not release clangd's whole index. Workspace mutations still
notify sessions of filesystem changes and reconcile changed compilation databases.
Explicit clangd tools use their existing `configurations` argument to select a
subset, for example
`clangd_diagnostics(path="project/src/main.cpp", configurations=["debug"])`.
Omitting that argument or passing `[]` selects all available configurations.

Workspace widgets load both resources independently. The source view lets you
select a configuration for semantic highlighting and inline diagnostic markers;
lexical C/C++ colors also cover keywords, comments, literals, directives, and nested
brackets. Diagnostic hover panels show messages and related locations on underlined
ranges. Symbol type/signature hover is not yet included in Workspace snapshots.
Diff annotations apply to new/context lines only. Resources shows the complete saved
JSON with copying; large JSON uses plain text to keep tab switching responsive.
JSON and Copy all preserve the original tool result.
Moves synchronize clangd sessions without collecting a diagnostic resource.
The standalone diagnostics widget uses severity cards with ranges and related notes;
hover renders Markdown descriptions and highlights C/C++ signature blocks.
Definitions, references, and workspace symbols group matches by file and show a
saved source excerpt when a location is selected. Workspace symbols also show their
kind and containing scope; document symbols form a collapsible document outline.
Managed excerpts are captured with the tool result; external `root/` locations have
no source preview because explicit external reads require confirmation.

## CMake profiles

CMake owns which configurations can supply clangd compilation contexts. After a
successful configure/generate, ForgeMCP reads `cmakeFiles-v1` from a new CMake
File API reply and records SHA-256 hashes of that build directory's configuration
inputs in `project/` and `storage/`. Hashes and dependency lists live in memory;
restart the server and run `cmake_configure` to confirm contexts again. Existing
File API replies and `compile_commands.json` alone do not confirm current inputs.

Workspace writes, edits, moves, deletions, and directory creation check known
inputs at the operation boundary. Actual input changes withdraw affected contexts
and notify clangd through the CMake subscription; identical content and ordinary
source/header edits keep contexts available unless those files are CMake inputs.
Known preset files are treated conservatively without resolving preset inheritance
or includes. CONFIGURE_DEPENDS globs conservatively invalidate on structural
changes; older File API replies without glob information use this policy for all
configurations. No project-wide dependency scan or CMake/glob parser is used.

Run `cmake_configure` to restore outdated or unknown contexts. `cmake_build` is
also allowed for outdated configurations: a new successful File API generation
can restore them even when later compilation fails. A successful build without
regeneration does not refresh the input baseline. Failed configure/generate cannot
reactivate old artifacts, and invalidation leaves build trees and databases intact.
Unchanged contexts and database contents retain their clangd sessions.

Checks occur only during Workspace mutations, configure, and build. There are no
filesystem watchers, polling, or background scans, and no guarantee of detecting
external editor/process changes. External `root/` dependencies are omitted from
hash tracking without elicitation or reads; changes to those inputs require an
explicit configure. Native preset contexts still require a known managed build
directory and a compilation database.

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

`cmake_run_test(executable, arguments=null, cwd="project/", timeout={"total":600})`
runs one already-built test executable directly, without a profile or CTest
registration. Executable and working-directory paths use `project/...` or
`storage/...` and must resolve inside their respective roots. The working directory
must exist; storage is not created automatically. On Windows, batch files are not
supported. Arguments are a list of literal strings, preserving spaces and empty
strings; NUL is rejected. For example:

```json
{
  "executable": "project/build/ninja-debug/fixture_tests.exe",
  "arguments": ["--fail"],
  "cwd": "project/build/ninja-debug",
  "timeout": {"total": 30}
}
```

The process inherits the server environment, closes stdin, and waits for completion.
It does not replay toolset/preset environments or CTest properties such as
`WILL_FAIL`, fixtures, or per-test timeouts. Exit zero means success; a nonzero
exit or process failure is reported in `error`. The single structured result
contains `executable`, `arguments`, `cwd`, `error`, and `process_id`; logs are read
through `process_get`. No JUnit or framework case report is inferred from output.

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

## Git operations

Git uses the executable and environment of `--git-toolset ID_OR_NAME` (default:
`system`). It must be installed in that toolset. ForgeMCP does not initialize,
clone, create, move, or remove worktrees. Run one instance in each desired worktree;
the admin panel exposes their independent tools and histories in separate tabs.
The configured workspace must be the repository's worktree root. Linked worktrees
work normally: Git itself accesses their shared repository metadata, without
extending Workspace access to external source files.

Read tools are `git_status`, `git_diff`, `git_log`, `git_show`, `git_blame`,
`git_branches`, `git_tags`, `git_remotes`, and `git_conflicts`. `git_diff` selects
`unstaged`, `staged`, or `revision` comparisons; revision comparisons require `base`
and optionally `target`. Log pages return a resolved revision and `next_skip`;
reuse both to keep pagination anchored. Show uses the first parent for merges.
Conflict results include available base/ours/theirs object IDs, modes, and UTF-8
text; binary or gitlink stages retain metadata without invented text.

`git_stage(paths)`, `git_unstage(paths)`, and `git_restore(paths, source=null)` use
explicit literal project paths. Restore discards working-file changes using the
index or the selected committed source. `git_commit(message, amend=false)` commits
the current index. `git_branch` creates/deletes local branches; force deletion is
explicit. `git_switch` optionally creates a branch. `git_stash` supports list,
save, apply, pop, and drop, returning one consistent action-result schema.

`git_fetch` selects a configured remote and optionally prunes remote-tracking
refs. `git_pull` defaults to fast-forward only; `strategy=merge` or `rebase` is
explicit. These operations never automatically stash local changes. `git_push`
pushes HEAD to one destination branch; replacing published history requires
`expected_remote_oid`, used as an explicit force-with-lease expectation. No raw
force-push or arbitrary Git command tool is exposed. Credentials come from the
operator's Git/SSH configuration; tools do not accept passwords or tokens.

`git_merge`, `git_rebase`, `git_cherry_pick`, and `git_revert` preserve unfinished
operations. Cherry-pick/revert accept a mainline parent for merge commits.
Resolve conflicts using Workspace tools, stage the resolutions, then call
`git_continue(action=continue|abort|skip)`. Skip applies only to operations that
support it. Normal installed Git hooks remain enabled. A nonzero command may
leave conflicts or other partial changes: mutation results report `error`, actual
resulting HEAD, unfinished operation, conflicts, and `process_ids`. Inspect status
after interruption before deciding whether to retry. A secondary synchronization
failure is reported separately from a successfully completed Git operation.

Mutations share Workspace serialization and coordinate with CMake and clangd.
CMake alone determines compilation-context eligibility and notifies clangd through
its existing subscription. Source notifications combine Git dirty paths and
committed tree changes; unknown changes use conservative invalidation. No external
file watcher or cross-instance execution queue is added.

Git output is bounded to 4 MiB per command; narrow the request when the limit is
exceeded. Each command has a 300-second deadline and each request a 620-second
deadline. Machine names/metadata must be UTF-8; patches explicitly report decoding
replacement. Full process transcripts retain their existing ProcessService owner.
`forgemcp://git/status` is the last captured status, not a live scan.
`forgemcp://git/commits/{revision}` serves previously captured log/show metadata
with completion over retained revisions. These snapshots, the Git monitor, and
history disappear when the instance stops. The Git App displays one invocation:
status separates staged and working changes, history puts commit messages first,
and patches show file groups and line numbers with a wrap toggle. Blame keeps source
and attribution together; conflict views distinguish base, ours, theirs, missing
stages, and binary content. Errors and analysis-refresh warnings appear before
the current repository state. Local search, expandable details, individual copying,
and complete original JSON remain available; the widget never executes Git actions.

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

## Understanding tool errors

Expected tool errors name the invalid argument or missing prerequisite and explain
how to correct it. Small catalogs list available values; larger catalogs point to
an existing listing tool such as `toolsets_list`, `cmake_profiles`,
`clangd_configurations`, `processes_overview`, or `git_remotes`. Unknown argument
names are rejected instead of being silently ignored, with spelling suggestions
when possible. Process transcript selector errors describe their valid JSON forms.

For example, an unknown profile identifies `profiles` and either lists its valid
names or recommends `cmake_profiles()`. A bad edit identifies `old_text` and suggests
reading the current file or choosing a unique fragment, without repeating source
text. Toolset installation and profile configuration errors explicitly require
operator setup; toolsets are discovered once at startup.

When a command started, use its returned process ID with `process_get` to read its
transcript. CMake profile failures and Git mutation failures remain in the result's
`error` fields even when the MCP exchange itself succeeded. A mutation may have
partially completed; inspect the returned state and `git_status` before repeating
Git operations. A synchronization warning after a completed operation does not mean
the commit or push should be repeated. Retained process and result-resource IDs
belong to the current server process and disappear when it stops.

## Tests

Tests that need a C/C++ workspace use the shared `cpp_acceptance_project` fixture from
`tests/conftest.py`. It copies the complete acceptance project into a fresh pytest
temporary directory for each test, so tests can modify their workspace without
changing repository files.

## Verify

```powershell
.\.venv\Scripts\python.exe -m build --wheel
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
```

See [docs/architecture.md](docs/architecture.md) for module boundaries and the
registration lifecycle. Repository rules for coding agents live in [AGENTS.md](AGENTS.md).
See [docs/releasing.md](docs/releasing.md) for distribution checks, GitHub Releases,
and the separate final PyPI publishing step.

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
