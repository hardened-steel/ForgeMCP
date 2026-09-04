# Git inspection Apps worker report

## Branch and implementation commit

- Branch: `codex/apps-git-inspection`
- Implementation commit: `1d7fb9d4646e85b1ff198ee2cc00593869a00540`
  (`Add read-only Git inspection Apps`)

## Tool and App resource mapping

| Existing tool | App resource | Asset |
| --- | --- | --- |
| `git__diff` | `ui://forgemcp/git/diff` | `git-diff.html` |
| `git__log` | `ui://forgemcp/git/history` | `git-history.html` |
| `git__list_branches` | `ui://forgemcp/git/history` | `git-history.html` |
| `git__show_commit` | `ui://forgemcp/git/source-history` | `git-source-history.html` |
| `git__blame` | `ui://forgemcp/git/source-history` | `git-source-history.html` |

Every binding has visibility `("model", "app")`. Each resource uses the
exact `text/html;profile=mcp-app` contract, an explicit empty CSP domain list,
`prefers_border=True`, and no permissions or dedicated domain.

## Delivered files

- `src/forgemcp/git/plugin.py`: registration only for the three inspection
  resources and five existing Git tool bindings. The accepted `git__status`
  binding remains unchanged.
- `frontend/git-inspection-apps/`: plain JavaScript/CSS sources, a local
  source-digest build, and a fast source-safety test.
- `src/forgemcp/apps/assets/git-diff.html`
- `src/forgemcp/apps/assets/git-history.html`
- `src/forgemcp/apps/assets/git-source-history.html`
- `tests/test_mcp_apps_git_inspection.py`

## Interaction model and safety

Diff and source-history patch views use a fixed terminal-style internal
viewport. Their patch line construction retains the received patch text and
whitespace exactly. History and blame offer local row selection only, updating
fixed detail strips; no mutation, checkout, comparison, fetch, or pagination
control is present. Results prefer `structuredContent` and accept the existing
one-item JSON text fallback.

All untrusted result data is inserted with DOM `textContent`. The authored
frontend contains no unsafe HTML sink, network API, resource read, storage,
navigation, model-context update, or UI-originated MCP/tool call.

## Validation completed

- `node frontend/git-inspection-apps/build.mjs --write`
- `node frontend/git-inspection-apps/build.mjs --check`
- `node frontend/git-inspection-apps/test.mjs`
- `pytest -q tests/test_mcp_apps_git_inspection.py tests/test_git_integration.py`
  — 15 passed
- `pytest -q tests/test_mcp_apps.py` — 11 passed
- `python -m compileall -q src/forgemcp/git tests/test_mcp_apps_git_inspection.py`
- `git diff --check`

## Integration notes

No integration-owned central registry, npm script, shared frontend source,
existing App test, status App, server projection, or documentation contract was
modified. This worker does not claim global Apps inventory acceptance.
