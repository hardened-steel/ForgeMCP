# Implement Debugger result Apps

Read `00-common-contract.md` first. Create branch `codex/apps-debugger` from the
common base.

## Tool coverage

Bind all 16 debugger tools:

- `debugger__status`
- `debugger__list_adapters`
- `debugger__launch`
- `debugger__stop`
- `debugger__set_breakpoints`
- `debugger__continue`
- `debugger__pause`
- `debugger__step_over`
- `debugger__step_in`
- `debugger__step_out`
- `debugger__threads`
- `debugger__stack_trace`
- `debugger__scopes`
- `debugger__variables`
- `debugger__evaluate`
- `debugger__events`

## Resources

Use three shared assets:

1. `ui://forgemcp/debugger/session`
   - status, adapters, launch/stop, breakpoints, continue/pause/steps, events
2. `ui://forgemcp/debugger/stack`
   - threads, stack trace
3. `ui://forgemcp/debugger/data`
   - scopes, variables, evaluate

Use explicit unique App names corresponding to those resources.

## UX

Session is a compact terminal state panel with session state, stop reason,
generation and safe adapter metadata. Action results are confirmations only;
there are no Continue/Pause/Step/Stop/Launch controls in the App. Breakpoints
and events use a fixed selectable list and detail strip. Event cursor state is
display-only.

Stack presents threads and frames in a dense fixed-height view. Workspace
frames show relative source/line/column; external/omitted frames retain their
normalized public representation. Local selection changes only the fixed detail
strip and never requests another stack page.

Data presents scopes/variables as a compact expandable tree using only items
already present in the result. Do not call `debugger__variables` from the UI to
expand a handle. If a returned item only contains an opaque child handle, show
that children are available but not loaded. Evaluate displays the requested
identifier and normalized value/type/result. Never label evaluate as safe.

Never expose or derive native thread/frame/variables IDs, PID, adapter path,
raw DAP messages or external paths. Public opaque handles remain opaque and
must not be stored beyond current rendering.

Inspect `src/forgemcp/debugger/models.py` and actual handler result shapes.
Preserve session/stop-generation semantics and terminal event behavior.

## Ownership

Edit only the Debugger plugin for App registrations. Add sources under
`frontend/debugger-apps/`, assets under `src/forgemcp/apps/assets/`, and tests in
`tests/test_mcp_apps_debugger.py`.

Finish with the worker report required by the common contract.

