# Integrate all parallel MCP Apps branches

Run this only after all six implementation branches are complete. Start from
current `main` in a clean integration branch such as
`codex/apps-all-integration`.

## Inputs

Merge these branches one at a time:

- `codex/apps-core-workspace`
- `codex/apps-cmake`
- `codex/apps-quality`
- `codex/apps-git-inspection`
- `codex/apps-clangd`
- `codex/apps-debugger`

Read every worker report and `00-common-contract.md` before resolving conflicts.

Worker reports are committed to their respective implementation branches under
`docs/agent-reports/mcp-apps-parallel/`:

- `codex/apps-core-workspace` → `01-core-workspace.md`
- `codex/apps-cmake` → `02-cmake.md`
- `codex/apps-quality` → `03-quality.md`
- `codex/apps-git-inspection` → `04-git-inspection.md`
- `codex/apps-clangd` → `05-clangd.md`
- `codex/apps-debugger` → `06-debugger.md`

Read each report from its branch before merging that branch. For example:

```powershell
git show codex/apps-cmake:docs/agent-reports/mcp-apps-parallel/02-cmake.md
```

Treat a missing report as an incomplete worker branch: do not merge it until
the worker has created and committed the expected report. After all branches
are merged, keep all six reports in the integration branch as audit evidence.

## Merge policy

- Preserve the accepted Git Status and Project Status Apps.
- Preserve the browser-free frontend workflow. Puppeteer/Chromium/browser
  harness must not return.
- Preserve all 72 existing tool contracts exactly.
- Prefer subsystem-owned plugin/source/assets from its worker.
- Do not silently drop a binding or asset to resolve a conflict.
- Keep one shared UI resource for each result family specified by the worker
  prompts; do not duplicate the official runtime per tool.

## Central integration work

After merging, update central files once:

- `frontend/package.json` scripts must write/check/test every subsystem asset in
  deterministic order;
- regenerate `frontend/package-lock.json` only if package metadata actually
  changed; no new dependencies are expected;
- update `tests/acceptance_manifest.py` App inventory so all 72 public tools
  have exactly one App binding while model-visible inventory stays 72;
- consolidate global Apps contract/packaging assertions in
  `tests/test_mcp_apps.py` without deleting subsystem tests;
- update `scripts/verify.ps1 -Mode Apps` only if needed to run all fast
  browser-free build/static/Python checks;
- update README, architecture, acceptance matrix, roadmap and create one new
  integration ADR covering shared result-family resources and the no-action UI
  policy;
- ensure every generated HTML asset is included in the wheel using the existing
  package-data mechanism.

If workers copied identical build logic, consolidate it into a small
`frontend/common/build-app.mjs` only during integration. Keep the renderer code
subsystem-local and do not create a frontend framework.

## Required inventory audit

Using the real production plugin composition and Apps-capable SDK session,
assert:

- exactly 72 model-visible tools;
- exactly 72 tools have one nested `_meta.ui.resourceUri` binding;
- every referenced `ui://` resource exists once with exact MIME and empty CSP;
- shared resource reuse matches the intended tool groups;
- clients without Apps receive the unchanged tool schemas, annotations and
  results;
- no tool gains a second binding;
- no orphan App resource or missing packaged asset exists.

## Safety and UX audit

- Recursively scan authored frontend sources for tool/resource/network calls and
  unsafe DOM sinks.
- Confirm no action buttons or UI-originated MCP calls.
- Confirm fixed geometry, 320 px responsive rules, keyboard focus/selection and
  bounded parsing through source/unit tests.
- Confirm code/diff/diagnostic text is rendered as text and preserves required
  whitespace.
- Confirm no raw argv/environment/executable/native IDs/external paths are
  introduced by presentation code.
- Manual Inspector review may sample representative resources; do not add an
  automated browser gate.

## Validation

Run once:

- `npm ci --prefix frontend`
- `npm run write:asset --prefix frontend`
- `npm run build --prefix frontend`
- `npm test --prefix frontend`
- all MCP Apps and directly affected plugin tests
- `python -m compileall -q src tests`
- `git diff --check`
- `scripts/verify.ps1 -Mode Apps`
- portable pytest once after the integration is stable

Do not run Live toolchain acceptance unless production backend behavior outside
App registration changed.

## Final report

Verdict: `ALL_TOOL_MCP_APPS_READY_FOR_VISUAL_REVIEW`

Report merge order/commits, any conflicts, complete tool-to-resource matrix,
asset count/total size, tests, unchanged tool inventory/contracts, absence of UI
calls, package evidence, git status and any tools whose public result could only
support a minimal confirmation view. Do not claim visual acceptance before the
user samples the integrated widgets in MCP Inspector.
