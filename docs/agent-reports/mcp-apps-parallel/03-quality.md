# Quality MCP Apps worker report

## Branch and implementation commit

- Branch: `codex/apps-quality`
- Implementation commit: `93ca7f076f2d833c80332a51c6223edadd8937d5`

## Tool and resource mapping

| Tools | MCP App resource |
| --- | --- |
| `quality__status`, `clang_format__check`, `clang_format__apply`, `clang_tidy__list_checks` | `ui://forgemcp/quality/overview` |
| `clang_tidy__run`, `sanitizer__parse_report` | `ui://forgemcp/quality/findings` |

All six bindings have visibility `("model", "app")`. Both resources use
`text/html;profile=mcp-app`, explicit empty CSP domain lists, no permissions or
domain, and `prefers_border=True`.

## Assets and changed implementation files

- `frontend/quality-apps/` contains the two plain-JavaScript views, CSS,
  template, source-digest build script, and Node tests.
- `src/forgemcp/apps/assets/quality-overview.html` and
  `src/forgemcp/apps/assets/quality-findings.html` are checked-in self-contained
  package assets.
- `src/forgemcp/quality/plugin.py` loads package assets and registers only the
  two resources and the six existing-tool bindings.
- `tests/test_mcp_apps_quality.py` covers registration, assets, metadata, and
  Apps/non-Apps SDK behavior.

## Interaction model and safety

The overview provides bounded tool/version status, snapshot-safe formatting
rows, and a fixed locally searchable clang-tidy check list. The findings view
provides bounded clang-tidy diagnostic selection and sanitizer finding/frame
selection in a fixed detail area. Both views render untrusted strings only via
DOM `textContent`, fail closed for malformed or oversized results, and display
only a relative suffix for diagnostic locations.

Neither App makes UI-originated MCP calls, resource reads, network requests,
storage access, navigation, or model-context updates. The interface has no
Fix, Apply, Run, Open File, or rerun action.

## Validation

- `node frontend/quality-apps/build.mjs --write`
- `node frontend/quality-apps/build.mjs --check`
- `node --test frontend/quality-apps/test.mjs` — 3 passed
- `pytest -q tests/test_mcp_apps_quality.py tests/test_quality_security.py tests/test_mcp_apps.py` — 76 passed, 2 skipped
- `python -m compileall -q src/forgemcp/quality tests/test_mcp_apps_quality.py`
- `git diff --check`

## Integration notes

None. No integration-owned files were changed, and no global Apps inventory
acceptance claim is made by this worker report.
