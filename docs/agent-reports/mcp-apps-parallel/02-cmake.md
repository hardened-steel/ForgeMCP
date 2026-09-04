# CMake MCP Apps worker report

Branch: `codex/apps-cmake`
Implementation commit: `ba66f6176269ca68ec89c5fcade66ea3ec74aa57`

## Tool and resource mapping

- `ui://forgemcp/cmake/catalog`: `cmake__status`, `cmake__list_kits`,
  `cmake__list_build_trees`, `cmake__list_presets`, `cmake__list_targets`,
  and `cmake__ctest_list_tests`.
- `ui://forgemcp/cmake/operation`: `cmake__select_kit`,
  `cmake__configure`, `cmake__build`, and `cmake__ctest_run`.

Every binding uses visibility `("model", "app")`. Both static resources use
`text/html;profile=mcp-app`, an explicit empty CSP domain list,
`prefers_border=True`, and no permissions or domain.

## Assets and interaction model

The package assets are `cmake-catalog.html` and `cmake-operation.html`, built
from the subsystem-local sources under `frontend/cmake-apps/`. Each is a
self-contained UTF-8 HTML5 asset with a source digest and uses the official
`@modelcontextprotocol/ext-apps` runtime.

The catalog App renders bounded safe status/list metadata and permits local
row selection in a fixed detail strip. The operation App distinguishes command
failure from malformed/transport results and displays only bounded normalized
diagnostics. Both retain fixed geometry at responsive breakpoints, use
`textContent` for all result data, and do not expose action buttons.

No UI-originated MCP calls, resource reads, network requests, navigation,
storage, clipboard, polling, or model-context updates were added.

## Changed files

- `src/forgemcp/cmake/plugin.py`
- `frontend/cmake-apps/build.mjs`
- `frontend/cmake-apps/catalog-app.js`
- `frontend/cmake-apps/operation-app.js`
- `frontend/cmake-apps/cmake-app.css`
- `frontend/cmake-apps/template.html`
- `frontend/cmake-apps/test.mjs`
- `src/forgemcp/apps/assets/cmake-catalog.html`
- `src/forgemcp/apps/assets/cmake-operation.html`
- `tests/test_mcp_apps_cmake.py`

## Validation

- `node frontend/cmake-apps/build.mjs --write`
- `node frontend/cmake-apps/build.mjs --check`
- `node --test frontend/cmake-apps/test.mjs`
- CMake App and affected CMake Python tests: `29 passed, 3 skipped`
- Python `compileall` for changed package/test paths
- `git diff --check`

## Integration notes

No integration-owned frontend scripts, shared App sources, global Apps tests,
acceptance manifest, server adapter, or existing documentation were changed.
This report does not claim global Apps inventory acceptance.
