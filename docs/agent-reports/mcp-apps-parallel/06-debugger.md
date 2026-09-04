# Debugger MCP Apps worker report

## Delivery

- Branch: `codex/apps-debugger`
- Implementation commit: `b9256a78eb9779e413bc6503120df092a29703d2` (`Add debugger MCP Apps`)

## App resources and bindings

| Resource | Bound tools |
| --- | --- |
| `ui://forgemcp/debugger/session` | `debugger__status`, `debugger__list_adapters`, `debugger__launch`, `debugger__stop`, `debugger__set_breakpoints`, `debugger__continue`, `debugger__pause`, `debugger__step_over`, `debugger__step_in`, `debugger__step_out`, `debugger__events` |
| `ui://forgemcp/debugger/stack` | `debugger__threads`, `debugger__stack_trace` |
| `ui://forgemcp/debugger/data` | `debugger__scopes`, `debugger__variables`, `debugger__evaluate` |

Every binding uses visibility `("model", "app")`. Resources use exact MIME
`text/html;profile=mcp-app`, explicit empty CSP domain lists, and
`prefers_border=True`; permissions and domain are omitted.

## Assets and interaction model

Generated package assets:

- `src/forgemcp/apps/assets/debugger-session.html`
- `src/forgemcp/apps/assets/debugger-stack.html`
- `src/forgemcp/apps/assets/debugger-data.html`

Frontend source, the source-digest build script, and focused Node tests are in
`frontend/debugger-apps/`. The session view is confirmation/status-only and has
no lifecycle controls. Breakpoint and event lists, stack rows, and scope or
variable rows support local selection only in a fixed detail area. Child data is
shown as available-but-not-loaded; the data view never expands a handle through
an MCP call. Evaluate retains the requested identifier only for rendering and
labels possible side effects rather than claiming safety.

All authored views accept `structuredContent` and one-item JSON text fallbacks,
bound data before rendering, use `textContent`, and make no UI-originated MCP,
network, storage, navigation, clipboard, or model-context calls. Opaque public
handles, native IDs, PIDs, adapter paths, raw DAP records, and external paths
are neither rendered nor retained.

## Changed files

- `src/forgemcp/debugger/plugin.py`
- `frontend/debugger-apps/`
- `src/forgemcp/apps/assets/debugger-session.html`
- `src/forgemcp/apps/assets/debugger-stack.html`
- `src/forgemcp/apps/assets/debugger-data.html`
- `tests/test_mcp_apps_debugger.py`

## Validation

- `node frontend/debugger-apps/build.mjs --write`
- `node frontend/debugger-apps/build.mjs --check`
- `node --test frontend/debugger-apps/test.mjs` (2 passed)
- `pytest -q tests/test_mcp_apps_debugger.py tests/test_debugger_service.py` (13 passed)
- `python -m compileall -q src/forgemcp/debugger tests/test_mcp_apps_debugger.py`
- `git diff --check`

## Integration notes

No integration-owned files were changed. The integration worker should add the
subsystem build/test command to any canonical frontend command and update the
central Apps inventory/acceptance coverage as needed.
