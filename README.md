# ForgeMCP

ForgeMCP is an MCP server that will provide AI assistants with deep, structured integration for C++ development.

## Current CMake and clangd slices

The server exposes a Core diagnostic tool and the built-in CMake feature plugin:

- `server_status`
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

`FORGEMCP_WORKSPACE` must name an existing workspace directory. The Core validates it but does not inspect project files.

Feature integrations use the public `forgemcp.plugins` contract. CMake and clangd are built-in plugins; clangd does not start a process until `clangd__start` receives an explicit workspace-contained directory with `compile_commands.json`. `FORGEMCP_CLANGD` optionally names an absolute clangd executable; otherwise the Process Runtime uses its policy-approved PATH discovery. External entry-point plugins are disabled by default; enabling them requires both `FORGEMCP_EXTERNAL_PLUGINS_ENABLED=true` and an explicit comma-separated `FORGEMCP_EXTERNAL_PLUGIN_ALLOWLIST`. See [architecture.md](docs/architecture.md), [ADR 0005](docs/adr/0005-feature-plugin-contract-and-external-trust.md), and [ADR 0007](docs/adr/0007-managed-lsp-lifecycle-document-synchronization-and-uri-policy.md) before allowing third-party code or extending clangd.

Debugger Phase 1 is launch-only source debugging through a separately installed
exact `FORGEMCP_LLDB_DAP` path. It supports workspace-contained PE/COFF + DWARF launch, source
breakpoints, execution control, paused inspection, one-identifier hover lookup
(which may still execute debugger/inferior evaluation semantics), and bounded events. Attach, MSVC/PDB compatibility claims, terminals, arbitrary
LLDB commands, and source/symbol downloads are intentionally unsupported; see
[ADR 0009](docs/adr/0009-dap-architecture-backend-and-debugger-trust-model.md).

Quality Phase 1 is a builtin, non-persistent feature plugin. It discovers
`clang-format` and `clang-tidy` from explicit absolute
`FORGEMCP_CLANG_FORMAT` / `FORGEMCP_CLANG_TIDY` configuration first, then the
policy-controlled PATH and conventional installed LLVM location. Relative,
empty, current-directory, and workspace PATH candidates are not quality-tool
approvals. Discovery records one canonical regular non-link executable with
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

## Core structure

- `core/config.py` — typed runtime configuration and workspace-root validation.
- `core/services.py` — explicit dependency registry for future modules.
- `core/application.py` — application composition, lifecycle, and server status.
- `plugins/` — versioned feature-plugin contract, lifecycle manager, tool registry, and opt-in entry-point discovery.
- `cmake/` — built-in CMake/Ctest feature plugin, CMake-owned models, and File API parsing.
- `lsp/` — reusable transport-neutral JSON-RPC/LSP framing and request client.
- `clangd/` — managed clangd feature plugin, normalized models, document synchronization, and safe WorkspaceEdit application.
- `quality/` — clang-format CAS edits, read-only clang-tidy diagnostics, and sanitizer report parsing.
- `core/errors.py` — expected Core errors and safe MCP-facing responses.
- `core/logging.py` — structured, redacted stderr logging.

See [architecture.md](docs/architecture.md) for Core boundaries and extension points.
