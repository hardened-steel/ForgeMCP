# Core and Workspace MCP Apps worker report

## Delivery

- Branch: `codex/apps-core-workspace`
- Implementation commit: `8939366fa244195a14734957d14ad9863471fe4a` (`Add Core and Workspace MCP Apps`)

## Tool and App-resource mapping

| Tool | App resource |
| --- | --- |
| `server_status` | `ui://forgemcp/server/status` |
| `workspace__list_files` | `ui://forgemcp/workspace/result` |
| `workspace__read_text` | `ui://forgemcp/workspace/result` |
| `workspace__get_snapshot` | `ui://forgemcp/workspace/result` |
| `workspace__apply_unified_patch` | `ui://forgemcp/workspace/result` |
| `workspace__apply_text_edits` | `ui://forgemcp/workspace/result` |

Both resources are static package assets with exact MIME
`text/html;profile=mcp-app`, empty CSP domain lists, no permissions/domain,
and `prefersBorder=true`. Workspace bindings use
`ToolAppBinding(..., visibility=("model", "app"))`.

## Assets and interaction model

- `src/forgemcp/apps/assets/server-status.html`
- `src/forgemcp/apps/assets/workspace-result.html`
- Sources, templates, local build and Node checks:
  `frontend/core-workspace-apps/`

The Server Status App presents lifecycle, version, the fixed stdio transport
label, configured-workspace marker, and registered-service count in a compact
terminal strip. The Workspace Result App classifies the actual public result
forms into file lists, text reads, snapshots, mutations, and safe error states.
It supports only local selection of files, changed paths, and source lines.
Both views use fixed dimensions and bounded internal scrollports; all received
strings are placed with `textContent`.

No UI-originated MCP calls, resource reads, network requests, storage,
navigation, action buttons, or model-context updates were added.

## Changed files

- `src/forgemcp/server.py`
- `src/forgemcp/workspace/plugin.py`
- `src/forgemcp/apps/assets/server-status.html`
- `src/forgemcp/apps/assets/workspace-result.html`
- `frontend/core-workspace-apps/*`
- `tests/test_mcp_apps_core_workspace.py`

## Validation

- `node frontend/core-workspace-apps/build.mjs --write`
- `node frontend/core-workspace-apps/build.mjs --check`
- `node --test frontend/core-workspace-apps/test.mjs` — 3 passed
- `python -m pytest -q tests/test_mcp_apps_core_workspace.py tests/test_workspace_plugin.py tests/test_server.py tests/test_mcp_apps.py` — 23 passed
- `python -m compileall -q src/forgemcp tests`
- `git diff --check`

## Integration notes

`ToolAppBinding` currently rejects `server_status` because its validation
requires a qualified tool name containing `__`. The Core resource and the
`server_status` App metadata are therefore registered directly in `server.py`
without a registry binding. A central, backwards-compatible exception for this
historical Core tool is required before it can be represented by a
`ToolAppBinding`; this worker deliberately did not edit the integration-owned
`src/forgemcp/plugins/apps.py`.
