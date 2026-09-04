# Common contract for parallel ForgeMCP App workers

Read this file completely before the assigned subsystem prompt.

## Scope and branch discipline

- Start from the same current `main` commit that contains these prompt files.
- Work only in the branch named by the subsystem prompt.
- Do not merge another Apps branch and do not rebase onto work from another
  parallel worker.
- Commit the completed subsystem branch and report its commit hash.
- Do not begin integration work.

To minimize conflicts, do not edit these integration-owned files unless the
subsystem prompt explicitly grants a narrow exception:

- `frontend/package.json`
- `frontend/package-lock.json`
- `frontend/common/*`
- `tests/test_mcp_apps.py`
- `tests/acceptance_manifest.py`
- `scripts/bootstrap.ps1`
- `scripts/verify.ps1`
- `README.md`
- `docs/architecture.md`
- `docs/acceptance-matrix.md`
- `docs/roadmap.md`
- existing ADR files
- `src/forgemcp/plugins/apps.py`
- `src/forgemcp/server.py`

If a central change appears necessary, record it under `Integration notes` in
the final report instead of implementing it.

## MCP contract

- Keep every existing tool name, input schema, output schema, annotations,
  handler, textual fallback and `structuredContent` behavior unchanged.
- Add only App resources and `ToolAppBinding` entries in the owning feature
  plugin.
- Bind with visibility `("model", "app")`.
- Use exact MIME `text/html;profile=mcp-app`.
- Use explicit empty CSP domain lists, `prefers_border=True`, and omit
  permissions/domain.
- Preserve identical behavior for clients without the Apps extension.
- Do not add model-visible tools, app-only helper tools, resources outside the
  requested `ui://` resources, prompts, completion providers or subscriptions.

## Frontend contract

- Use the already installed official `@modelcontextprotocol/ext-apps` runtime.
- Reuse `frontend/common/theme.css` and `frontend/common/mcp-app.js` read-only.
- Plain JavaScript and CSS only. Do not add a framework or dependency.
- Register handlers before `connect()` and pass an explicit unique App name to
  `connectMcpApp`.
- Accept the real public result shape. Prefer `structuredContent`; support the
  existing one-item JSON text fallback when a tool does not publish structured
  content.
- Treat all tool input/result strings as untrusted data and insert them with
  `textContent` only.
- Do not use `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`,
  `eval`, `Function`, inline event attributes or unsafe template concatenation.
- No `callServerTool`, `tools/call`, resource reads, fetch, XHR, WebSocket,
  navigation, storage, clipboard, fullscreen, polling or model-context update.
- No action buttons. Controls may only filter, select, expand details locally,
  or switch between already received result sections.
- Fixed geometry per responsive breakpoint. Interaction must not resize the
  widget. Long lists/code/diffs use a fixed internal viewport or fixed detail
  area.
- Match the compact terminal style of the existing Git and Project Status Apps:
  mono font, small type/padding, restrained color, dense rows, fixed detail
  strip, no decorative dashboard cards.
- Support dark/light host themes, host fonts, safe-area insets, 736 px and 320 px
  widths, keyboard access and visible focus.
- Pair status color with a symbol and textual state.
- Enforce bounded item counts and string lengths before rendering. Malformed,
  partial or oversized results render a compact fail-closed state without a
  JavaScript exception.

## Assets and build

- Add subsystem frontend sources under the directory named by the subsystem
  prompt.
- Add a subsystem-local `build.mjs` supporting `--write` and `--check`.
- It may copy the small established build pattern, but must consume the shared
  theme/lifecycle sources and official runtime. Do not modify the common files.
- Generate the exact assets named by the subsystem prompt under
  `src/forgemcp/apps/assets/`.
- Each asset is self-contained HTML5, UTF-8, single-file, source-digest checked,
  package-resource loadable and below the existing App byte limit.
- Do not add Puppeteer, Chromium, Playwright, jsdom, browser automation or a
  render harness.
- Do not modify central npm scripts. The integration worker will wire all
  subsystem build scripts into the canonical frontend commands.

## Tests

- Add a new subsystem-specific Python test file; do not modify shared Apps
  tests.
- Add a subsystem-local fast Node test only when it verifies real pure logic,
  source safety, asset syntax/freshness or bounded result classification.
- Test every requested tool-to-resource binding, URI uniqueness within the
  subsystem, exact MIME/CSP, packaged asset loading, no authored calls/network,
  safe DOM construction and representative public result shapes.
- Verify non-Apps fallback remains unchanged using existing server behavior or
  focused plugin/registry tests without duplicating the full global SDK suite.
- No browser tests and no full Portable/Live acceptance run.

## Required worker validation

- Run every subsystem `build.mjs --write`, then `build.mjs --check`.
- Run subsystem Node tests.
- Run the new subsystem Python test file and directly affected existing feature
  tests.
- Run `python -m compileall -q` for changed Python packages/tests.
- Run `git diff --check`.
- Do not run `scripts/verify.ps1`; it is intentionally integration-owned.

## Worker report

Report branch and commit, tools and shared resource mapping, assets, changed
files, interaction model, tests, confirmation of no UI-originated MCP calls,
and integration notes. Do not claim global Apps inventory acceptance.
