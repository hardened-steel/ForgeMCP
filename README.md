# ForgeMCP

ForgeMCP is an MCP server that will provide AI assistants with deep, structured integration for C++ development.

## Current MCP surface

The server exposes a Core diagnostic tool and the built-in CMake feature plugin:

- `server_status`
- `workspace__list_files`, `workspace__read_text`, `workspace__get_snapshot`
- `workspace__apply_unified_patch`, `workspace__apply_text_edits`
- `project__status`
- `cmake__status`
- `cmake__list_kits`, `cmake__select_kit`, `cmake__list_build_trees`
- `cmake__list_presets`
- `cmake__configure`
- `cmake__list_targets`
- `cmake__build`
- `cmake__ctest_list_tests`
- `cmake__ctest_run`
- `clangd__status`
- `clangd__start`
- `clangd__stop`
- `clangd__diagnostics`
- `clangd__hover`
- `clangd__definition`
- `clangd__references`
- `clangd__document_symbols`
- `clangd__workspace_symbols`
- `clangd__completion`
- `clangd__signature_help`
- `clangd__declaration`, `clangd__type_definition`, `clangd__implementation`
- `clangd__prepare_rename`, `clangd__rename`
- `clangd__code_actions`, `clangd__apply_code_action`
- `clangd__format_document`, `clangd__format_range`
- `clangd__prepare_call_hierarchy`, `clangd__incoming_calls`, `clangd__outgoing_calls`
- `clangd__prepare_type_hierarchy`, `clangd__supertypes`, `clangd__subtypes`
- `clangd__switch_source_header`
- `debugger__status`, `debugger__list_adapters`, `debugger__launch`, `debugger__stop`
- `debugger__set_breakpoints`, `debugger__continue`, `debugger__pause`, `debugger__step_over`, `debugger__step_in`, `debugger__step_out`
- `debugger__threads`, `debugger__stack_trace`, `debugger__scopes`, `debugger__variables`, `debugger__evaluate`, `debugger__events`
- `quality__status`
- `clang_format__check`, `clang_format__apply`
- `clang_tidy__list_checks`, `clang_tidy__run`
- `sanitizer__parse_report`
- `git__status`, `git__diff`, `git__log`, `git__show_commit`, `git__blame`, and
  `git__list_branches` (Git Intelligence Phase 1, read-only only)

UX Stabilization Phase C also exposes application-scoped discovery data:

- resources: `forgemcp://about`, `forgemcp://project/status`,
  `forgemcp://workspace/files`, `forgemcp://cmake/targets`,
  `forgemcp://git/status`, and
  `forgemcp://logs/recent`;
- resource templates: `forgemcp://workspace/files/{cursor}`,
  `forgemcp://cmake/targets/{profile}`, and
  `forgemcp://logs/recent/{level}/{limit}`; and
- prompts: `forgemcp_build_report`, `forgemcp_test_report`,
  `forgemcp_diagnose_build`, `forgemcp_analyze_file`, and
  `forgemcp_debug_target`, plus `forgemcp_review_changes`.

Every discovery resource is versioned bounded `application/json`. The Git
Status App separately exposes the static `ui://forgemcp/git/status` resource as
`text/html;profile=mcp-app`; it carries no project data and is rendered only by
clients that negotiate the stable MCP Apps extension. Project-controlled
filenames, targets, test names, and safe log metadata are untrusted JSON data,
not instructions. Resources never return source text, patch text, raw process
output, diagnostic messages, argv/environment, absolute paths, PIDs, handles,
or raw exceptions.

`--workspace` / `FORGEMCP_WORKSPACE` must name an existing workspace directory. The Core validates it but does not inspect project files.

Feature integrations use the public `forgemcp.plugins` contract. Workspace, CMake, and clangd are built-in plugins; clangd does not start a process until `clangd__start` is called. It uses an explicit workspace-contained `compile_commands.json` directory when supplied, otherwise the latest CMake-validated profile (or fallback commands with policy `off`). `--clangd` / `FORGEMCP_CLANGD` may name an exact executable; otherwise the application-scoped Toolchain Discovery Service uses its common safe selection rules. External entry-point plugins are disabled by default; enabling them requires both `FORGEMCP_EXTERNAL_PLUGINS_ENABLED=true` and an explicit comma-separated `FORGEMCP_EXTERNAL_PLUGIN_ALLOWLIST`. See [architecture.md](docs/architecture.md), [ADR 0005](docs/adr/0005-feature-plugin-contract-and-external-trust.md), [ADR 0007](docs/adr/0007-managed-lsp-lifecycle-document-synchronization-and-uri-policy.md), and [ADR 0014](docs/adr/0014-workspace-mutations-and-compilation-database-coherence.md) before allowing third-party code or extending clangd.

`project__status {}` is the sole Project Intelligence Phase 1 operation. It
concurrently aggregates bounded cached snapshots for Core, Workspace, Process
Runtime, Plugin Manager, CMake, clangd, debugger, Quality, and cached Git. It never refreshes
tools, reads source, starts a process/session, runs a build/test/analysis, or
changes lifecycle. Provider failure yields an explicit partial response without
raw exceptions. Overlapping calls share one in-flight snapshot, cancellation
cleanup is bounded, and the UTF-8 JSON response is capped at 100,000 bytes with
deterministic omission metadata. Provider models are strictly revalidated at
the registry boundary, including timestamp freshness. Health is separate from activity and the per-component
timestamps make the result intentionally non-transactional. Git contributes
only cached scalar availability/count data and never probes from project status;
diagnostic messages, raw output, persistent history, and multi-workspace
aggregation remain absent. See [ADR 0011](docs/adr/0011-project-status-provider-and-health-model.md)
and [ADR 0017](docs/adr/0017-git-read-only-trust-and-repository-boundary.md).

Debugger Phase 1 is launch-only source debugging through a separately installed
exact `--lldb-dap` / `FORGEMCP_LLDB_DAP` path or one exact discovered LLVM candidate. It supports workspace-contained PE/COFF + DWARF launch, source
breakpoints, execution control, paused inspection, one-identifier hover lookup
(which may still execute debugger/inferior evaluation semantics), and bounded events. Attach, MSVC/PDB compatibility claims, terminals, arbitrary
LLDB commands, and source/symbol downloads are intentionally unsupported; see
[ADR 0009](docs/adr/0009-dap-architecture-backend-and-debugger-trust-model.md).

Quality Phase 1 is a builtin, non-persistent feature plugin. It receives
`clang-format` and `clang-tidy` from the common discovery service, whose
priority is explicit CLI/environment, active Developer environment, selected
Visual Studio, safe PATH, then conventional LLVM location. Relative, empty,
current-directory, and workspace PATH candidates are not quality-tool approvals.
Discovery records one canonical regular non-link executable with
metadata and every probe/run rechecks and launches that exact path. Missing tools
do not prevent server startup. Formatting sends the captured UTF-8 snapshot on
stdin with a validated `--assume-filename`, parses bounded replacement XML byte
offsets into Unicode code-point edits, and uses snapshot-CAS with one staged
Workspace commit; ForgeMCP never invokes `clang-format -i`. UTF-8 BOM is
supported and preserved. `clang-tidy` accepts only a validated workspace build
directory containing regular non-link `compile_commands.json`, exposes no fixes,
plugin loading, compiler-argument, config, response-file, or arbitrary-flag MCP
surface, and treats the workspace/CMake compilation database as trusted project
input. The database can itself contain frontend/plugin flags and external include
paths; ForgeMCP is not a sandbox. The sanitizer tool parses supplied ASan/UBSan
text read-only and never runs a binary or symbolizer.
See [ADR 0010](docs/adr/0010-quality-tools-formatting-analysis-and-trust-boundary.md).

## Setup

Requires Python 3.11 or later.

### C++ acceptance fixture and D2 gates

`examples/cpp-acceptance-project` is the permanent, dependency-free C++
acceptance project. It contains successful static-library/application/debug
targets, a warning target, intentionally broken compile/link targets, CTest
cases, clangd/format/tidy anchors, and synthetic sanitizer reports. Build only
a disposable copy: generated build directories, compile databases, binaries,
PDBs, and clangd caches are ignored and must never be committed.

The portable fixture workflow is `cmake --preset ninja-debug`, then
`cmake --build --preset build-ninja-debug`, and `ctest --preset test-ninja-debug`.
`fixture_compile_error` and `fixture_link_error` are `EXCLUDE_FROM_ALL`;
negative CTest cases require the `ninja-debug-negative-tests` preset. Select
ForgeMCP kits through `cmake__list_kits` and `cmake__select_kit`, or use a
CMake preset—those are alternative workflows and a combined explicit
preset/kit is rejected. The full LLVM/DWARF debugger profile requires a
qualified Clang kit and `lldb-dap`; MSVC/PDB is not claimed.

Run the complete real-clangd MCP fixture gate (SDK stdio, production discovery,
all currently published `clangd__*` tools) with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m clangd_fixture_mcp
```

It first discovers clangd and a ready standalone LLVM kit through ForgeMCP.
It can skip only when that production discovery is unavailable; a qualified
host runs against a disposable copy and verifies the committed fixture hash.

Run the complete real debugger MCP fixture gate (SDK stdio, production
discovery, all currently published `debugger__*` tools) with:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -m debugger_fixture_mcp
```

It selects a ready standalone Clang kit using only the public kit identity,
builds `fixture_debug` with `FIXTURE_LLVM_DWARF=ON` through CMake MCP, and
qualifies the standalone LLDB-DAP adapter through MCP. The gate covers paused
inspection and stepping plus running pause/stop and sequential-session handle
expiry. Identifier hover evaluation remains potentially side-effecting even
though the public policy only accepts one ASCII identifier.

Always-run tests verify fixture content, ignored artifacts, and the real MCP
surface/matrix. The portable live gate runs automatically whenever CMake and
Ninja are available; see [the acceptance matrix](docs/acceptance-matrix.md)
for tool-by-tool scope and the unit/fake/live distinction.

Run the complete host-qualified acceptance tier with one switch:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --run-forgemcp-live-acceptance
```

It uses production-equivalent Toolchain Discovery and writes path-free,
host-local capability and bounded dynamic SDK-coverage JSON reports under the
temporary directory (never the repository). It runs MSVC gates for a ready
MSVC kit and standalone LLVM/clangd/Quality/DAP gates for their qualified
chain, failing if a discovered capability is skipped or has no meaningful SDK
call. Plain `pytest -q` remains portable and conditionally skips only proven
absent native capabilities. Resources and prompts are discovery aids, not
authority; progress is best-effort. Build/test/debug execute trusted workspace
code, and adopted IDE build trees remain validated project input—not a sandbox
or trust grant.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[test]"
```

`mcp` is the only runtime dependency. To start the server for a C++ workspace:

```powershell
$env:FORGEMCP_WORKSPACE = "C:\path\to\cpp-project"
forgemcp
```

CMake 3.23 or later is supported. Configure, build, and test intentionally run
the selected project's CMake logic and test executables; run ForgeMCP only for
workspaces whose project code you trust. See [ADR 0006](docs/adr/0006-cmake-file-api-build-directory-and-trust-boundary.md).

## Configuration and CLI (Phase A)

`forgemcp` without a subcommand remains the stdio MCP server. Configuration is
assembled once at application composition; feature plugins never read the host
environment directly. The exact precedence is:

1. a parameter on the individual safe MCP operation;
2. the corresponding CLI option;
3. the corresponding `FORGEMCP_*` environment variable;
4. automatic toolchain/preset discovery;
5. the documented default.

CLI therefore always overrides environment. `forgemcp print-config` and
`forgemcp doctor --json` report source categories only (`cli`, `environment`,
`discovery`, `default`): they never print environment values, secrets, or
absolute host executable paths.

```powershell
forgemcp --help
forgemcp --workspace C:\src\demo --build-dir build doctor
forgemcp --workspace C:\src\demo print-config
# Backward-compatible stdio server:
forgemcp --workspace C:\src\demo --build-dir build
```

| CLI option | Environment variable | Default | Allowed values | Purpose | Security remarks |
| --- | --- | --- | --- | --- | --- |
| `--workspace DIR` | `FORGEMCP_WORKSPACE` | current directory | existing directory | Workspace root | Validated once; never emitted as a host path. |
| `--source-dir DIR` | `FORGEMCP_SOURCE_DIR` | `.` | workspace-relative directory | Default CMake source tree | Workspace policy and symlink checks apply. |
| `--build-dir DIR` | `FORGEMCP_BUILD_DIR` | preset `binaryDir`, then `build` | workspace-relative directory | Default CMake build tree | Every variant is workspace-contained and symlink-safe. |
| `--cmake PATH` | `FORGEMCP_CMAKE` | discovery | local absolute executable | CMake executable | Exact regular non-link/reparse file, never in workspace; UNC/device paths are rejected. |
| `--ctest PATH` | `FORGEMCP_CTEST` | discovery | local absolute executable | CTest executable | Same exact-file policy. |
| `--clangd PATH` | `FORGEMCP_CLANGD` | discovery | local absolute executable | clangd executable | Same exact-file policy; no flags are configurable. |
| `--clang-format PATH` | `FORGEMCP_CLANG_FORMAT` | discovery | local absolute executable | clang-format executable | Same exact-file policy; `-i` remains unavailable. |
| `--clang-tidy PATH` | `FORGEMCP_CLANG_TIDY` | discovery | local absolute executable | clang-tidy executable | Same exact-file policy; no arbitrary arguments/fixes. |
| `--lldb-dap PATH` | `FORGEMCP_LLDB_DAP` | discovery | local absolute executable | LLVM DAP adapter | Exact-file qualification and strict adapter policy still apply. |
| `--git PATH` | `FORGEMCP_GIT` | discovery | local absolute executable | Read-only Git Intelligence | Exact regular non-link/reparse file outside the workspace; an invalid explicit value never falls back. |
| `--toolchain MODE` | `FORGEMCP_TOOLCHAIN` | `auto` | `auto`, `msvc`, `llvm` | Compiler-family preference | `llvm` prefers ready standalone `clang++`; exact provider remains path-free and is selected by CMake kit. |
| `--host-arch ARCH` | `FORGEMCP_HOST_ARCH` | `auto` | `auto`, `x64`, `x86`, `arm64` | Tool process architecture | Incompatible PE candidates are rejected. |
| `--target-arch ARCH` | `FORGEMCP_TARGET_ARCH` | `auto` | `auto`, `x64`, `x86`, `arm64` | MSVC compiler target | Used only in fixed VS developer-environment setup. |
| `--visual-studio-instance SELECTOR` | `FORGEMCP_VISUAL_STUDIO_INSTANCE` | deterministic eligible instance | exact bounded instance ID, product, display-name, or version | Select a VS instance | Paths and command fragments are rejected; no selector enters a shell command. |
| `--cmake-generator NAME` | `FORGEMCP_CMAKE_GENERATOR` | none | bounded name | Generator outside a preset | Mutually exclusive with a configured preset. |
| `--cmake-kit ID` | `FORGEMCP_CMAKE_KIT` | automatic | opaque kit ID | Initial ForgeMCP CMake kit | Path-free ID from `cmake__list_kits`; selection is application-local. |
| `--configure-preset NAME` | `FORGEMCP_CONFIGURE_PRESET` | none | bounded name | Default configure preset | Its direct `binaryDir` is rechecked in workspace policy. |
| `--configuration NAME` | `FORGEMCP_DEFAULT_CONFIGURATION` | none | bounded name | Default multi-config configuration | Passed as one argv value only. |
| `--compile-commands MODE` | `FORGEMCP_COMPILE_COMMANDS` | `auto` | `auto`, `required`, `off` | CMake compilation database policy | `required` rejects unsupported/missing databases; `off` uses clangd fallback commands. |
| `--configure-timeout-sec N` | `FORGEMCP_CONFIGURE_TIMEOUT_SEC` | `300` | `0 < N <= 3600` | Configure timeout | Process Runtime remains the execution boundary. |
| `--build-timeout-sec N` | `FORGEMCP_BUILD_TIMEOUT_SEC` | `900` | `0 < N <= 3600` | Build timeout | Process Runtime remains the execution boundary. |
| `--test-timeout-sec N` | `FORGEMCP_TEST_TIMEOUT_SEC` | `900` | `0 < N <= 3600` | CTest timeout | Per-call safe timeout may override it. |
| `--log-level LEVEL` | `FORGEMCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | stderr logging level | Values never enter MCP stdio. |
| `--external-plugins-enabled` / `--no-external-plugins` | `FORGEMCP_EXTERNAL_PLUGINS_ENABLED` | `false` | boolean | Existing opt-in plugin switch | Both switch and allow-list are still required. |
| `--external-plugin-allowlist NAMES` | `FORGEMCP_EXTERNAL_PLUGIN_ALLOWLIST` | empty | comma-separated entry-point IDs | Existing external plugin allow-list | Names only; no plugin paths or environment values are exposed. |

The local commands are `forgemcp doctor`, `forgemcp doctor --json`, and
`forgemcp print-config`. `doctor` checks, without enabling extra DAP backends,
`cmake`, `ctest`, `ninja`, `MSBuild`, `cl`, `clang`, `clang++`, `clangd`,
`clang-format`, `clang-tidy`, and `lldb-dap`; it reports only sanitized
availability/rejection reasons. `cppvsdbg` and `OpenDebugAD7` are discovery-only
candidates and are never selected automatically.

`doctor --json` emits one bounded JSON object with exactly `configuration` and
`discovery` sections. It contains fixed tool IDs, source/rejection categories,
and configuration source categories only; it never contains the inherited PATH,
raw environment values, secrets, executable paths, VS instance IDs, or other
host paths. Plain `doctor` is local operator output and may show the same safe
categories, but is not an MCP response.

### Workspace MCP tools and compilation database coherence

Workspace tools accept only bounded workspace-relative UTF-8 paths and files:
absolute, drive-relative, UNC/device, alternate-data-stream, traversal,
symlink/junction/reparse, ignored-directory, and binary-file access are
rejected. File listings are capped at 1,000 entries. Start with
`workspace__read_text` or `workspace__get_snapshot`; pass the returned
SHA-256 to every mutation. Existing files require that CAS value, while patch
creation requires an explicit expected-absent `null`. A validation or snapshot
conflict changes nothing; no-op edits publish no change. A text-edit batch has
at most 1,000 edits, and patch/replacement input plus aggregate staged output
are byte-bounded before any write. Delete and rename are intentionally not
public tools. Both the published input and success/error result schemas forbid
unknown fields. Source is returned only by `read_text` and is never logged,
placed in status/progress/errors, or put on the mutation bus.

Successful Workspace commits publish exactly one deterministically path-ordered,
content-free, application-local batch after the filesystem lock and staging
cleanup are released. Subscriber failure, timeout, or queue saturation never
undoes a commit, but is sticky `DEGRADED` integration state; CMake is then
conservatively stale and clangd synchronizes on the next safe request. Active
clangd documents receive one full-text `didChange` for the committed snapshot;
untracked documents remain lazy. CMakeLists, `.cmake`, preset, and configured
in-workspace toolchain mutations mark CMake configuration stale but never
auto-configure. There is no external filesystem watcher.

`--compile-commands` resolves CLI over `FORGEMCP_COMPILE_COMMANDS` over the
default `auto`; the only modes are `auto`, `required`, and `off`.
`auto` adds `CMAKE_EXPORT_COMPILE_COMMANDS=ON`. It selects Ninja only when
there is no explicit generator or preset, no generator in an existing cache,
the generated tree is empty, and qualified Ninja plus a compatible selected
toolchain environment are available (the discovered MSVC Developer environment
on Windows). Ninja, Ninja Multi-Config, and the named CMake Makefile families
are the only claimed database-capable generators; Visual Studio generators are
never replaced and are not claimed to produce a database. `required` rejects
an explicit export `OFF` and unavailable/invalid databases; `off` permits
clangd fallback commands. CMake validates only a bounded regular UTF-8
database inside the selected build tree and shares metadata/fingerprint only.
An active clangd session is controlled-restarted only when that fingerprint
changes. A CMake mutation after configure starts leaves its result stale even
if the process succeeds. See [ADR 0014](docs/adr/0014-workspace-mutations-and-compilation-database-coherence.md).

### CMake build-directory resolution

For all CMake operations, omitted `binary_dir` resolves as follows:

```text
tool-call binary_dir → CLI --build-dir → FORGEMCP_BUILD_DIR
→ selected configure preset binaryDir → workspace-relative build
```

`cmake__status` includes the resulting workspace-relative source/build profile
and its safe source category. Preset, CLI, and environment choices use the same
Workspace policy; none can select a build tree outside the workspace.

ForgeMCP only derives a preset build directory from a direct, unconditional
`binaryDir` using the supported `${sourceDir}` macro. Inheritance, conditions,
other macros, and missing `binaryDir` are left to CMake; if ForgeMCP needs a
pre-configure build location in those cases, the caller must supply `binary_dir`
explicitly. ForgeMCP does not claim that a Visual Studio generator produces
`compile_commands.json`.

### CMake Kits and existing build trees (Phase D1)

`cmake__list_kits {}` returns cached immutable ForgeMCP kits. A kit is a
path-free toolchain profile: opaque ID, compiler family/version identity,
origin, driver mode, ABI marker,
host/target architecture, safe Visual Studio identity/version, filtered
environment-profile category, compatible/preferred generators, compilation
database capability, debugger compatibility, readiness, and fixed reasons.
Compiler paths, installation paths, Developer environment values, raw probe
output, and commands are never returned through MCP. Select one with
`cmake__select_kit {"kit":"kit-..."}`; this changes only the current
application session, increments a CAS generation, does not configure, delete
cache files, or mutate ForgeMCP's process environment. Initial selection order
is configure `kit`, runtime selection, `--cmake-kit`, `FORGEMCP_CMAKE_KIT`,
then deterministic qualified automatic selection.

Generator precedence is configure `generator`, CLI/environment generator,
existing build-tree generator, preset-owned generator, selected-kit preference,
then safe automatic selection. A kit's Ninja preference is never allowed to
rewrite an existing generator. For command-line generators ForgeMCP supplies
private canonical C/C++ compiler paths and the filtered kit environment to
CMake; Visual Studio generators use their native generator/platform semantics.

ForgeMCP kits have a similar purpose to CMake Tools Kits but are not its
serialized format. ForgeMCP never reads VS Code global storage, active-kit
state, user `cmake-tools-kits.json`, `.vscode/settings.json`, setup scripts,
environment variables, or command fields. `.vscode/cmake-kits.json` is an
unsupported external format. CMake Presets are a separate standard workflow;
an explicit preset and explicit ForgeMCP kit are rejected as a structured
conflict rather than silently mixed.

`cmake__list_build_trees {}` performs a bounded read-only scan of conventional
workspace build directories (`build`, `build-*`, `cmake-build-*`,
`out/build/*`, configured/known profiles). It can adopt a compatible existing
tree—including one created by VS Code—using ordinary target/build/test work,
validated File API, and a valid compile database. It never identifies VS Code
as the owner. Incompatible source/generator/compiler metadata is reported and
never reconfigured or deleted. With an explicit kit and no binary directory,
the deterministic suggestion is `build/forgemcp/<safe-kit-id>`; separate
binary directories prevent unsafe kit/generator cache switching.

### Timeout scopes

`FORGEMCP_CONFIGURE_TIMEOUT_SEC`, `FORGEMCP_BUILD_TIMEOUT_SEC`, and
`FORGEMCP_TEST_TIMEOUT_SEC` are ForgeMCP operation limits. The safe CTest
operation timeout may override its configured default, but it remains bounded
by Process Runtime policy. Codex `tool_timeout_sec` is an independent client
deadline and must be high enough for the operation; an MCP server cannot extend
it. Progress notifications never extend, replace, or reset either deadline.
For configure/build/test work, use `tool_timeout_sec = 1800` in the Codex
server entry unless a project has a deliberately smaller operating limit.

### MCP progress (UX Stabilization Phase B)

When the MCP client attaches a progress token, ForgeMCP reports request-scoped
progress for `cmake__configure`, `cmake__build`, `cmake__ctest_run`,
`clang_tidy__run`, `clangd__start`, `clangd__stop`, `debugger__launch`, and
`debugger__stop`. Clients without a token, or clients whose progress transport
is unavailable, receive the same result/error and the operation continues.
MCP string tokens, numeric tokens, and numeric `0` are preserved verbatim per
request; tokens never enter ForgeMCP models, logs, errors, or status data.

Configure/build/test publish fixed safe phases and a bounded elapsed heartbeat
while the tool is quiet. Ninja's strict `[completed/total]` form and strict
CTest completion lines provide exact progress only when they are unambiguous;
MSBuild and unknown generators deliberately remain heartbeat-only. Progress
is monotonic across phase, heartbeat, exact, and terminal notifications. A
successful exact operation reserves `total/total` for its terminal success;
failure, timeout, and cancellation preserve the last observed value instead.
Labels are short normalized status text. They never contain command argv,
absolute paths, environment values, raw process output, source text, or
secrets. Only model-validated, length-checked build target labels may be
shown; project-controlled CTest output never contributes a test-name label.

Delivery is rate-limited and synchronous per request (there are no unbounded
notification tasks). Each request has an independent reporter; slow/failing
progress delivery is disabled for that call rather than blocking a child
process. A successful long operation sends a terminal update; failure,
timeout, and cancellation send a terminal category without claiming 100%.
Progress is not MCP Logging and is never mirrored as a raw log event. It does
not extend a ForgeMCP operation timeout or the client's `tool_timeout_sec`.

### MCP discovery, prompts, completion, and logging (UX Stabilization Phase C)

The initialize response carries 904 UTF-8 bytes of static ForgeMCP-authored
server-wide guidance. Its first 512 characters are self-contained: start with
`project__status`, prefer Workspace/CMake operations to direct filesystem or
shell mutation, use clangd/debugger/Quality for their respective domains,
obtain a snapshot and CAS guard before editing, and treat workspace execution
as trusted code. Instructions contain no host path, discovered executable,
environment value, or project file content. They are MCP guidance, not a claim
that the host installs a true system-role message. [Official OpenAI
documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) says Codex
reads the MCP `instructions` initialization field; other MCP clients can apply,
display, truncate, or ignore it differently.

The workspace manifest returns at most 1,000 deterministic metadata entries in
pages of 50. Its application-local opaque cursor expires after five minutes,
is invalid across ForgeMCP applications, and becomes stale after a ForgeMCP
Workspace mutation. External filesystem changes can still make a multi-page
walk slightly inconsistent; the resource explicitly does not call it a
transactional snapshot. Cached CMake target profiles are opaque, expire after
ten minutes, and never cause configure, File API query creation, or a process
launch. Project status uses the same side-effect-free cached-provider contract
as `project__status`.

Prompt handlers only render messages. They never call a tool. Prompt arguments
are bounded identifiers, are strictly checked, and appear separately as
JSON-quoted untrusted data. Completion supports prefix filtering, deterministic
deduplication, context from already-filled prompt arguments, at most 100
values, and `total`/`hasMore`. The legacy MCP `completion/complete` method can
complete only `PromptReference` and `ResourceTemplateReference`; it cannot
complete arbitrary tool JSON parameters. Static tool choices therefore remain
`Literal`/enum schema values, while dynamic tool inputs are discovered through
the existing list/status tools.

Logging has four deliberately separate channels:

- `FORGEMCP_LOG_LEVEL` controls only the structured JSON stderr sink;
- `logging/setLevel` controls only the current connection's bounded
  `notifications/message` queue and supports `debug` through `emergency`;
- the recent-log resource reads a 256-event/512-KiB application-local sanitized
  ring with no notification replay; and
- request progress remains request-scoped and is never duplicated as logging.

MCP log delivery has one sender, a 64-event queue, a 20-event/second ceiling,
and a 500 ms send deadline. A slow or disconnected client cannot block tool
execution. Reading logs does not log the read, and `project__status` does not
emit logs as a side effect. Resource subscriptions/change notifications are not
advertised in Phase C: SDK 1.x reports `subscribe=false`, and ForgeMCP does not
add an ad-hoc subscription transport.

Connection Info should show Tools, Resources, Prompts, Logging, and Completions;
it also advertises `extensions.io.modelcontextprotocol/ui={}` for MCP Apps.
Tasks remain unsupported and Experimental is absent. ForgeMCP intentionally
uses the SDK-supported legacy `2025-11-25` stdio protocol. “Legacy” describes
the protocol era and is not an initialization error; ForgeMCP does not layer a
custom modern wire protocol over MCP Python SDK 1.x. Client support for listing,
displaying, or invoking resources and prompts can vary.

### Windows and Codex examples

Run the local check before registering the server:

```powershell
forgemcp --workspace C:\src\demo --build-dir build doctor
codex mcp add demo-cpp -- forgemcp --workspace C:\src\demo --build-dir build
```

A project-scoped `.codex/config.toml` can pass options through `args` (Windows
TOML backslashes must be escaped):

```toml
[mcp_servers.forgemcp]
command = "forgemcp"
args = ["--workspace", "C:\\src\\demo", "--build-dir", "build", "--toolchain", "msvc"]
tool_timeout_sec = 1800
startup_timeout_sec = 30
```

Inspect the same stdio server with the official MCP Inspector. Because current
Inspector CLI parsing does not reliably forward leading-dash server arguments,
the environment form is the simplest ad-hoc Windows example:

```powershell
$env:FORGEMCP_WORKSPACE = "C:\src\demo"
npx @modelcontextprotocol/inspector forgemcp
npx @modelcontextprotocol/inspector --cli forgemcp -- --method resources/list
npx @modelcontextprotocol/inspector --cli forgemcp -- --method prompts/list
npx @modelcontextprotocol/inspector --cli forgemcp -- --method tools/call --tool-name git__status --app-info
npx @modelcontextprotocol/inspector --cli forgemcp -- --method resources/read --uri ui://forgemcp/git/status --format json
npx @modelcontextprotocol/inspector --cli forgemcp -- --method tools/call --tool-name git__status --format json
```

The Inspector can exercise resources, prompts, completion, and logging even if
a particular model host does not render all of them. Its current command and
configuration forms are documented in the [official Inspector
repository](https://github.com/modelcontextprotocol/inspector).

### Test-only environment switches

These are never read by the server configuration and must not be used for MCP
deployment. They exist solely for explicitly opted-in integration fixtures and
gates in this repository.

| Variable | Purpose |
| --- | --- |
| `FORGEMCP_REAL_WINDOWS_TOOLCHAIN_GATE` | Enables the real Windows VS discovery/MSVC/CMake/CTest gate with a cleared PATH. |
| `FORGEMCP_LLDB_DAP_LIVE_TEST` | Supplies lldb-dap to existing opt-in DAP tests. |
| `FORGEMCP_LLVM_CLANG_LIVE_TEST` | Supplies clang to existing opt-in DAP compile tests. |
| `FORGEMCP_CLANG_FORMAT_LIVE_TEST` | Supplies clang-format to existing live Quality tests. |
| `FORGEMCP_CLANG_TIDY_LIVE_TEST` | Supplies clang-tidy to existing live Quality tests. |
| `FORGEMCP_PROJECT_STATUS_FIXTURE` | Selects only the test stdio fixture behavior. |
| `FORGEMCP_TEST_VALUE`, `FORGEMCP_TEST_SECRET`, `FORGEMCP_TEST_NORMAL` | Process Runtime test fixtures; never runtime settings. |

The alternative is an `env` block; CLI still wins if both are present:

```toml
[mcp_servers.forgemcp]
command = "forgemcp"
env = { FORGEMCP_WORKSPACE = "C:\\src\\demo", FORGEMCP_BUILD_DIR = "build", FORGEMCP_TOOLCHAIN = "msvc" }
tool_timeout_sec = 1800
startup_timeout_sec = 30
```

## Git Intelligence Phase 1

Git Intelligence Phase 1 is intentionally read-only. `git__status` uses
porcelain v2, `git__diff` exposes only staged or unstaged bounded patches, and
`git__log`, `git__show_commit`, `git__blame`, and `git__list_branches` use
fixed local-only grammars. There are no stage/unstage, commit, checkout,
branch mutation, merge/rebase/reset/clean, fetch/pull/push, credential, or
arbitrary-argv operations.

Git is qualified as one exact canonical regular non-link executable, never a
workspace executable. Every invocation uses `ProcessRuntime`, `shell=False`,
an exact rechecked executable, bounded output/timeout/cancellation cleanup,
`--no-optional-locks`, disabled pager/prompt, a scrubbed Git environment,
disabled fsmonitor, and `--no-ext-diff`/`--no-textconv` where patches are
generated. No Git config, host path, raw argv/output, patch, commit message,
author, or filename reaches ForgeMCP logs or ProjectStatus.

The workspace must itself be the Git worktree root. Normal `.git` directories
and linked-worktree `.git` files are supported, including an internal gitdir
outside the workspace, but that metadata path is never exposed. Git paths are
revalidated through WorkspaceService; nested repositories and submodules are
not traversed. Patch text and commit/branch/author/path metadata are untrusted
project data, not instructions. Git is not a sandbox, and Phase 1 performs no
network operation. `project__status` consumes only the cached scalar Git
summary and never probes Git.

### Git Status MCP App

Apps-capable hosts which declare
`extensions.io.modelcontextprotocol/ui.mimeTypes=["text/html;profile=mcp-app"]`
receive nested metadata on `git__status`:

```json
{
  "_meta": {
    "ui": {
      "resourceUri": "ui://forgemcp/git/status",
      "visibility": ["model", "app"]
    }
  }
}
```

The resource uses the exact App MIME, explicit empty CSP domain lists, omitted
permissions/domain, and `prefersBorder=true`. It renders repository state,
bounded HEAD, divergence/counts, warnings, and grouped safe-text file rows with
All/Staged/Modified/Untracked/Conflicted filters. Refresh calls only
`git__status`; there are no App-only writes. Hosts without the extension retain
the unmodified plain tool schema, structured content, and textual fallback.

The checked-in source and offline build are under `frontend/git-status`. Node
is not a ForgeMCP runtime dependency:

```powershell
Push-Location frontend\git-status
npm run build
npm test
Pop-Location
```

`npm run build` regenerates `src/forgemcp/apps/assets/git-status.html`; the
embedded source SHA-256 makes drift fail the frontend test. See
[ADR 0018](docs/adr/0018-mcp-apps-sdk1-compatibility-and-git-status-security.md).

## Core structure

- `core/config.py` — typed runtime configuration and workspace-root validation.
- `core/services.py` — explicit dependency registry for future modules.
- `core/application.py` — application composition, lifecycle, and server status.
- `plugins/` — versioned feature-plugin contract, lifecycle manager, bounded tool/resource/prompt/completion/App registries, and opt-in entry-point discovery.
- `discovery/` — static server instructions, safe workflow prompts, about/log resources, and completion declarations.
- `cmake/` — built-in CMake/Ctest feature plugin, CMake-owned models, and File API parsing.
- `lsp/` — reusable transport-neutral JSON-RPC/LSP framing and request client.
- `clangd/` — managed clangd feature plugin, normalized models, document synchronization, and safe WorkspaceEdit application.
- `quality/` — clang-format CAS edits, read-only clang-tidy diagnostics, and sanitizer report parsing.
- `git/` — bounded porcelain/protocol parsing, fixed read-only Git service, and GitPlugin MCP contributions.
- `apps/assets/` — immutable built MCP App HTML loaded from package resources, never workspace paths.
- `project/` — strict status models, provider registry, health/activity aggregation, and ProjectPlugin contribution.
- `core/errors.py` — expected Core errors and safe MCP-facing responses.
- `core/logging.py` — application-scoped sanitized fan-out, JSON stderr sink, and bounded recent-log ring.

See [architecture.md](docs/architecture.md) for Core boundaries and extension points.
