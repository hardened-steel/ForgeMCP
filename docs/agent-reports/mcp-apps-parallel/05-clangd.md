# clangd result Apps worker report

## Branch and commit

- Branch: `codex/apps-clangd`
- Implementation commit: `e43365cc0826ede1477ba58b92001140886b061b`

## Tool and resource mapping

| Resource | Bound tools |
| --- | --- |
| `ui://forgemcp/clangd/session` | `clangd__status`, `clangd__start`, `clangd__stop` |
| `ui://forgemcp/clangd/insight` | `clangd__diagnostics`, `clangd__hover`, `clangd__completion`, `clangd__signature_help` |
| `ui://forgemcp/clangd/navigation` | `clangd__definition`, `clangd__references`, `clangd__declaration`, `clangd__type_definition`, `clangd__implementation`, `clangd__document_symbols`, `clangd__workspace_symbols`, `clangd__switch_source_header` |
| `ui://forgemcp/clangd/change-hierarchy` | `clangd__prepare_rename`, `clangd__rename`, `clangd__code_actions`, `clangd__apply_code_action`, `clangd__format_document`, `clangd__format_range`, `clangd__prepare_call_hierarchy`, `clangd__incoming_calls`, `clangd__outgoing_calls`, `clangd__prepare_type_hierarchy`, `clangd__supertypes`, `clangd__subtypes` |

All bindings use `("model", "app")`. All resources use exact MIME
`text/html;profile=mcp-app`, explicit empty CSP domain lists, and
`prefers_border=True` without permissions or domain metadata.

## Assets and interaction model

Added four source-digest-checked, package-loadable single-file assets:

- `clangd-session.html`
- `clangd-insight.html`
- `clangd-navigation.html`
- `clangd-change-hierarchy.html`

Their source and local build/test code are under `frontend/clangd-apps/`.
The compact terminal views use safe DOM construction and `textContent` only.
They support local selection of returned rows, diagnostic/completion/symbol
inspection, preformatted hover text, and hierarchy/change summaries. They do
not perform UI-originated MCP tool calls, resource reads, network requests,
storage, navigation, model-context updates, or mutations.

## Changed files

- `src/forgemcp/clangd/plugin.py`
- `src/forgemcp/apps/assets/clangd-session.html`
- `src/forgemcp/apps/assets/clangd-insight.html`
- `src/forgemcp/apps/assets/clangd-navigation.html`
- `src/forgemcp/apps/assets/clangd-change-hierarchy.html`
- `frontend/clangd-apps/`
- `tests/test_mcp_apps_clangd.py`

## Validation

- `node frontend/clangd-apps/build.mjs --write`
- `node frontend/clangd-apps/build.mjs --check`
- `node --test frontend/clangd-apps/test.mjs` — 2 passed
- `pytest -q tests/test_mcp_apps_clangd.py tests/test_clangd_integration.py tests/test_mcp_apps.py` — 49 passed
- `python -m compileall -q src/forgemcp/clangd tests/test_mcp_apps_clangd.py`
- `git diff --check`

## Integration notes

The parallel-worker contract prohibits editing central frontend scripts and the
shared Apps inventory. Integration should wire `frontend/clangd-apps/build.mjs`
and `test.mjs` into canonical frontend commands, then extend the shared Apps
inventory with these four clangd resources. No integration work was performed.
