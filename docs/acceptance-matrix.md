# D2.4 acceptance matrix

The committed C++ fixture is copied to a test-owned temporary directory before
every mutation. No acceptance test writes the committed example. The live
inventory is obtained with real MCP `initialize` and `tools/list`; the test
rejects missing, extra, or duplicate manifest names.

| Surface | Coverage | Reason / scenario |
| --- | --- | --- |
| Core/workspace/CMake/Quality (23 tools) | real SDK stdio | `core_fixture.*`, `cmake_fixture.*`, and `quality_fixture.*` cover normal and expected-error fixture paths. |
| Git (6 tools) | real SDK stdio | `git_fixture.*` initializes a disposable local repository and calls all six tools; staged/unstaged/untracked/rename/deletion/binary/local branch cases are asserted with no remote. |
| clangd (27 tools) | real SDK stdio | `clangd_fixture.*` selects a ready standalone LLVM kit, configures Ninja, validates the database, and calls every published `clangd__*` tool. |
| debugger (16 tools) | real SDK stdio | `debugger_fixture.*` qualifies LLDB-DAP, uses the public `debugger__step_over` name, and covers paused/running stop paths. MSVC/PDB is not claimed. |
| Existing build trees | real SDK stdio and hostile unit matrix | An IDE-like external Ninja/Clang tree with CRLF cache, File API, and compilation database is created before the ForgeMCP session. Listing is hash-proved read-only; targets, build, CTest, and status follow without configure. Unit cases reject mismatch, stale/malformed/oversized metadata, source mismatch, and workspace escape. |
| Progress | SDK stdio and adversarial parser/transport tests | CMake/CTest, numeric-zero/string/no-token isolation, strict exactness, heartbeats, cancellation, slow delivery, partial UTF-8/final line, and observer overflow are covered. clangd, clang-tidy, and debugger lifecycle progress share the request-scoped contract. |
| Discovery/disclosure | SDK stdio and recursive sanitization tests | Initialize, resources/templates, prompts, completion, logging, pagination/TTL/invalidation, bounded queues, and canary-safe channel separation are covered. |
| MCP Apps Git Status | SDK stdio Apps/no-Apps, static UI source checks, wheel smoke, and Inspector v2 | The Apps client sees the stable extension, nested `git__status` metadata, exact `ui://forgemcp/git/status` MIME, empty CSP lists, no permissions, and normal tool result. The non-Apps client keeps the same plain tool/structured fallback. HTML lifecycle, one-request Refresh, no-network/no-unsafe-DOM checks, and XSS canaries are covered without changing the 72-tool manifest. |

`tests/acceptance_manifest.py` is the machine-readable per-tool manifest. The
grouping above is explanatory only; its exact mapping is checked against the
real server surface.

For each clangd and debugger entry the manifest also records a fixture anchor, meaningful
success assertion, setup condition, and cleanup assertion. The scenario call
collector records every official `ClientSession.call_tool` invocation; a listed
clangd tool cannot be counted merely because `tools/list` advertised it.

The machine-readable manifest is dynamic from the declared tool inventory
(currently **72 tools**): 23 Core/workspace/CMake/Quality, 6 Git, 27 clangd,
and 16 debugger. The unified runner wraps official SDK
`call_tool` (never handlers/services), emits one bounded host-local record per
tool/scenario with call count, meaningful-success state, and
success/expected-error category, and rejects duplicates/orphans. `tools/list`,
imports, direct service calls, and `optional_platform_gate` cannot satisfy
coverage. Use:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --run-forgemcp-live-acceptance
```

Its capability report includes only availability/source categories and public
kit families—never installation or executable paths. Portable `pytest -q`
retains skips only where production discovery proves a capability absent.
