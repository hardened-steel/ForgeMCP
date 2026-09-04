# Implement Core and Workspace result Apps

Read `00-common-contract.md` first. Create branch
`codex/apps-core-workspace` from the common base.

## Tool coverage

Bind all six tools:

- `server_status`
- `workspace__list_files`
- `workspace__read_text`
- `workspace__get_snapshot`
- `workspace__apply_unified_patch`
- `workspace__apply_text_edits`

## Resources

Use two bounded assets:

- `server_status` → `ui://forgemcp/server/status`
- all five `workspace__*` tools → `ui://forgemcp/workspace/result`

Suggested App names: `forgemcp-server-status` and
`forgemcp-workspace-result`.

## UX

Server status is a very small terminal strip: lifecycle, version, transport and
safe scalar status. No invented health score.

Workspace result classifies the received public result shape:

- file list: dense relative-path table with type/size/hash metadata, local
  selection and a fixed details line;
- read text: fixed-height code/text viewport with line numbers, path/snapshot
  metadata and local line selection; preserve whitespace and never reinterpret
  content as HTML;
- snapshot: compact metadata view;
- mutation result: applied/no-op/conflict summary and affected relative files,
  with no patch reconstruction and no action controls.

Use fixed dimensions. Long content belongs in the internal code viewport, not
in an expanding outer panel. Do not add filesystem actions, copy buttons or
links.

Inspect actual models in `src/forgemcp/workspace/plugin.py` and
`src/forgemcp/models/files.py`; do not infer fields.

## Ownership

You may edit the Core/server tool owner and Workspace plugin only as needed to
register the two App resources and bindings. Do not change handlers or models.
Add frontend sources under `frontend/core-workspace-apps/`, assets with clear
names under `src/forgemcp/apps/assets/`, and focused tests in
`tests/test_mcp_apps_core_workspace.py`.

Finish with the worker report required by the common contract.

