# ForgeMCP

ForgeMCP is an MCP server that will provide AI assistants with deep, structured integration for C++ development.

## Current CMake and clangd slices

The server exposes a Core diagnostic tool and the built-in CMake feature plugin:

- `server_status`
- `workspace__list_files`, `workspace__read_text`, `workspace__get_snapshot`
- `workspace__apply_unified_patch`, `workspace__apply_text_edits`
- `project__status`
- `cmake__status`
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

`--workspace` / `FORGEMCP_WORKSPACE` must name an existing workspace directory. The Core validates it but does not inspect project files.

Feature integrations use the public `forgemcp.plugins` contract. Workspace, CMake, and clangd are built-in plugins; clangd does not start a process until `clangd__start` is called. It uses an explicit workspace-contained `compile_commands.json` directory when supplied, otherwise the latest CMake-validated profile (or fallback commands with policy `off`). `--clangd` / `FORGEMCP_CLANGD` may name an exact executable; otherwise the application-scoped Toolchain Discovery Service uses its common safe selection rules. External entry-point plugins are disabled by default; enabling them requires both `FORGEMCP_EXTERNAL_PLUGINS_ENABLED=true` and an explicit comma-separated `FORGEMCP_EXTERNAL_PLUGIN_ALLOWLIST`. See [architecture.md](docs/architecture.md), [ADR 0005](docs/adr/0005-feature-plugin-contract-and-external-trust.md), [ADR 0007](docs/adr/0007-managed-lsp-lifecycle-document-synchronization-and-uri-policy.md), and [ADR 0014](docs/adr/0014-workspace-mutations-and-compilation-database-coherence.md) before allowing third-party code or extending clangd.

`project__status {}` is the sole Project Intelligence Phase 1 operation. It
concurrently aggregates bounded cached snapshots for Core, Workspace, Process
Runtime, Plugin Manager, CMake, clangd, debugger, and Quality. It never refreshes
tools, reads source, starts a process/session, runs a build/test/analysis, or
changes lifecycle. Provider failure yields an explicit partial response without
raw exceptions. Overlapping calls share one in-flight snapshot, cancellation
cleanup is bounded, and the UTF-8 JSON response is capped at 100,000 bytes with
deterministic omission metadata. Provider models are strictly revalidated at
the registry boundary, including timestamp freshness. Health is separate from activity and the per-component
timestamps make the result intentionally non-transactional. Git, diagnostic
messages, raw output, persistent history, and multi-workspace aggregation are
not part of Phase 1; see [ADR 0011](docs/adr/0011-project-status-provider-and-health-model.md).

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
| `--toolchain MODE` | `FORGEMCP_TOOLCHAIN` | `auto` | `auto`, `msvc`, `llvm` | Discovery preference | Does not enable a new debugger backend. |
| `--host-arch ARCH` | `FORGEMCP_HOST_ARCH` | `auto` | `auto`, `x64`, `x86`, `arm64` | Tool process architecture | Incompatible PE candidates are rejected. |
| `--target-arch ARCH` | `FORGEMCP_TARGET_ARCH` | `auto` | `auto`, `x64`, `x86`, `arm64` | MSVC compiler target | Used only in fixed VS developer-environment setup. |
| `--visual-studio-instance SELECTOR` | `FORGEMCP_VISUAL_STUDIO_INSTANCE` | deterministic eligible instance | exact bounded instance ID, product, display-name, or version | Select a VS instance | Paths and command fragments are rejected; no selector enters a shell command. |
| `--cmake-generator NAME` | `FORGEMCP_CMAKE_GENERATOR` | none | bounded name | Generator outside a preset | Mutually exclusive with a configured preset. |
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

## Core structure

- `core/config.py` — typed runtime configuration and workspace-root validation.
- `core/services.py` — explicit dependency registry for future modules.
- `core/application.py` — application composition, lifecycle, and server status.
- `plugins/` — versioned feature-plugin contract, lifecycle manager, tool registry, and opt-in entry-point discovery.
- `cmake/` — built-in CMake/Ctest feature plugin, CMake-owned models, and File API parsing.
- `lsp/` — reusable transport-neutral JSON-RPC/LSP framing and request client.
- `clangd/` — managed clangd feature plugin, normalized models, document synchronization, and safe WorkspaceEdit application.
- `quality/` — clang-format CAS edits, read-only clang-tidy diagnostics, and sanitizer report parsing.
- `project/` — strict status models, provider registry, health/activity aggregation, and ProjectPlugin contribution.
- `core/errors.py` — expected Core errors and safe MCP-facing responses.
- `core/logging.py` — structured, redacted stderr logging.

See [architecture.md](docs/architecture.md) for Core boundaries and extension points.
