# Implement Quality result Apps

Read `00-common-contract.md` first. Create branch `codex/apps-quality` from the
common base.

## Tool coverage

Bind all six tools:

- `quality__status`
- `clang_format__check`
- `clang_format__apply`
- `clang_tidy__list_checks`
- `clang_tidy__run`
- `sanitizer__parse_report`

## Resources

Use two shared assets:

- `quality__status`, both clang-format tools and `clang_tidy__list_checks` →
  `ui://forgemcp/quality/overview`
- `clang_tidy__run` and `sanitizer__parse_report` →
  `ui://forgemcp/quality/findings`

Suggested App names: `forgemcp-quality-overview` and
`forgemcp-quality-findings`.

## UX

Overview renders:

- tool availability/version as a compact terminal matrix;
- format check/apply as per-file changed/applied/no-op/conflict rows with
  snapshot-safe metadata only;
- clang-tidy check names in a fixed searchable local list.

Findings renders a dense diagnostics/stack view:

- severity/category counts in one summary line;
- selectable clang-tidy diagnostics with relative location, code/source and
  normalized message;
- selectable sanitizer findings with bounded frames and a fixed frame detail
  area;
- explicit incomplete/truncated/execution state.

No Fix, Apply, Run, Open File or rerun buttons. Do not reconstruct raw sanitizer
input, compiler output, source excerpts, absolute paths or hidden external
frames.

Inspect `src/forgemcp/quality/models.py` and actual handlers. Preserve byte/code
point semantics and existing trust boundary.

## Ownership

Edit only the Quality plugin for registrations. Add sources under
`frontend/quality-apps/`, assets under `src/forgemcp/apps/assets/`, and tests in
`tests/test_mcp_apps_quality.py`.

Finish with the worker report required by the common contract.

