# D2 acceptance matrix

The committed C++ fixture is copied to a test-owned temporary directory before
every mutation. No acceptance test writes the committed example. The live
inventory is obtained with real MCP `initialize` and `tools/list`; the test
rejects missing, extra, or duplicate manifest names.

| Surface | Coverage | Reason / scenario |
| --- | --- | --- |
| `server_status`, `project__status` | real | Real stdio status and sanitized response checks. |
| Workspace read/snapshot/mutation tools | real | Read, creation patch, CAS edit, stale-CAS rejection, re-read. |
| All `cmake__*` tools | real | Cached discovery plus Ninja Debug configure/build/CTest. |
| All Quality tools | real | Disposable format/tidy fixture and synthetic report parsing. |
| Every `clangd__*` tool | real SDK stdio gate | `pytest -m clangd_fixture_mcp` obtains the exact live inventory, selects a ready standalone LLVM kit, configures Ninja, validates the generated database, and calls every tool on a disposable fixture copy. Qualified discovery forbids a skip. |
| Every `debugger__*` tool | optional platform gate | Needs qualified Windows PE/COFF + DWARF Clang build and compatible `lldb-dap`; MSVC/PDB is not claimed. |

`tests/acceptance_manifest.py` is the machine-readable per-tool manifest. The
grouping above is explanatory only; its exact mapping is checked against the
real server surface.

For each clangd entry the manifest also records a fixture anchor, meaningful
success assertion, setup condition, and cleanup assertion. The scenario call
collector records every official `ClientSession.call_tool` invocation; a listed
clangd tool cannot be counted merely because `tools/list` advertised it.
