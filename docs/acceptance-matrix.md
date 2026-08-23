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
| Every `clangd__*` tool | optional platform gate | Needs real compatible clangd and generated compile database; fixture supplies Phase 1/2 semantic anchors. |
| Every `debugger__*` tool | optional platform gate | Needs qualified Windows PE/COFF + DWARF Clang build and compatible `lldb-dap`; MSVC/PDB is not claimed. |

`tests/acceptance_manifest.py` is the machine-readable per-tool manifest. The
grouping above is explanatory only; its exact mapping is checked against the
real server surface.
