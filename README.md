# ForgeMCP

ForgeMCP is a Python MCP server for structured C and C++ development workflows.
The repository currently contains a deliberately small vertical slice that establishes
the conventions for future workspace, CMake, clangd, quality, and debugger modules.

## Current MCP surface

- Workspace tools browse directory trees, find files, read UTF-8 text and file
  metadata, search literal text or regex, write/edit/move/delete files, and create
  directories. Each tool reports progress and has a packaged MCP App and icon.
- Workspace resources mirror UTF-8 text and raw bytes, and expose directory trees,
  file lists, metadata, and search results as Markdown. Resource parameters have
  completions for roots, paths, extensions, depth, and boolean options.
- No prompts are currently registered.
- Tool `processes_overview` shows running and completed external development tools.
- Resource template `forgemcp://processes/{process_id}` exposes the retained state and
  text transcript of one process.
- Tool `toolsets_list` lists every discovered toolset; `toolset_get(toolset_id)` returns
  its absolute executable paths, kinds, and versions. Both open the toolsets widget.
- Resources `forgemcp://toolsets` and `forgemcp://toolsets/{toolset_id}` expose the
  toolsets as markdown, querying available versions for details, with completion
  for retained toolset IDs.

The tools remain useful in clients without MCP Apps support because the Python SDK
serializes their typed results into both text `content` and `structuredContent`.

All widgets share a compact console style with a fixed 420px height and adapt to the
host width. Long values wrap and remain selectable; overflow scrolls vertically.
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

All file tools accept `root="project"` (default) or `root="storage"`; their paths
are relative to that root. Storage defaults to `.<project-name>.forgemcp` beside the
project. Set its location explicitly when needed:

```powershell
forgemcp --workspace C:\Projects\Example --workspace-storage D:\ForgeMCP\Example
```

Storage is created on first use. Its persistent directories survive restarts;
internal temporary-directory contexts clean up their own folders on exit. The
process service permits working directories inside either configured root. This is
a working-directory check, not an operating-system sandbox.

| Tool | Operation |
| --- | --- |
| `workspace_list(path=".", depth=1)` | Directory tree; `depth=null` expands the whole tree; a file path is an error |
| `workspace_find_files(pattern="*", path=".", extensions=null)` | Recursive filename/path glob search |
| `workspace_file_info(path)` | Creation/modification times, byte size, owner; unavailable metadata is null |
| `workspace_read_file(path, start_line=1, end_line=null)` | UTF-8 text, with an optional inclusive line range |
| `workspace_search(query, path=".", regex=false, extensions=null, case_sensitive=true)` | Matching lines and skipped binary/non-UTF-8 files |
| `workspace_write_file(path, text)` | Create or overwrite; return removed/added line counts |
| `workspace_edit_file(path, old_text, new_text, replace_all=false)` | Exact replacement; zero or ambiguous matches fail without modifying the file |
| `workspace_move(source, destination)` | Move a file or directory; destination must not exist |
| `workspace_delete(path)` | Delete a file or empty directory |
| `workspace_mkdir(path)` | Create a directory and missing parents |

Text is UTF-8; writes use a sibling temporary file and `os.replace`. There are no
revision/hash parameters. Edits preserve text outside the replacement, including
line endings. Parents must already exist for file writes and moves.

Searches skip directories beginning with a dot and do not follow links. Ordinary
`build` directories and dot-prefixed files are searchable. Explicit file reads may
access hidden directories and in-root links. Mutations cannot traverse links;
moving/removing a whole tree containing links is rejected. Dependent modules may
register protected paths, which remain readable but cannot be changed through the
workspace API. Searches intentionally have no application-level timeout, result
limit, or pagination in this iteration.

Resource templates (the URI root is always explicit):

```text
forgemcp://workspace/{root}/file{/path*}
forgemcp://workspace/{root}/raw{/path*}
forgemcp://workspace/{root}/list{?path,depth}
forgemcp://workspace/{root}/find-files{?pattern,path,extensions}
forgemcp://workspace/{root}/file-info{?path}
forgemcp://workspace/{root}/search{?query,path,regex,extensions,case_sensitive}
```

Examples: `forgemcp://workspace/project/file/src/main.cpp`,
`forgemcp://workspace/storage/raw/build/debug/app.exe`, and
`forgemcp://workspace/project/search?query=TODO&extensions=cpp,hpp`.
Use `depth=all` for a full resource tree. Extensions are comma-separated;
an omitted parameter means any extension and `extensions=` means extensionless
files. Encode query values using percent encoding (spaces as `%20`, not `+`);
regex is supported. Text mirrors return the original text, raw mirrors return MCP
binary content, and the other four resources return `text/markdown`.

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
