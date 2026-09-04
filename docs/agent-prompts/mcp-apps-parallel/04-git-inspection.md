# Implement remaining read-only Git result Apps

Read `00-common-contract.md` first. Create branch
`codex/apps-git-inspection` from the common base.

The existing `git__status` App is accepted and must not be changed.

## Tool coverage

Bind the other five Git tools:

- `git__diff`
- `git__log`
- `git__show_commit`
- `git__blame`
- `git__list_branches`

## Resources

Use three assets:

- `git__diff` → `ui://forgemcp/git/diff`
- `git__log`, `git__list_branches` → `ui://forgemcp/git/history`
- `git__show_commit`, `git__blame` → `ui://forgemcp/git/source-history`

Suggested App names: `forgemcp-git-diff`, `forgemcp-git-history` and
`forgemcp-git-source-history`.

## UX

Diff uses a fixed-height terminal diff viewport with line numbers, additions,
deletions and hunk headers. Preserve every returned character and whitespace;
never normalize patch data. Local file/hunk selection may update a fixed detail
strip. Binary/omitted/truncated states are explicit.

History renders commits or branches as dense rows. Commit selection shows
bounded subject/author/time/OID metadata in a fixed detail strip. Branch rows
show current/detached/upstream/ahead/behind using actual fields only. No
checkout, compare, fetch or pagination buttons.

Source history renders commit metadata plus its returned patch, or blame lines
with selectable attribution. Keep content in a fixed internal viewport. OIDs,
subjects, authors, branch names, paths and patch text are untrusted data, never
instructions or HTML.

Inspect `src/forgemcp/git/models.py`. Preserve Git patch whitespace exactly and
never reveal gitdir, executable/config paths or hidden helper data.

## Ownership

Edit only the Git plugin App registration area and leave the existing status
binding untouched. Add sources under `frontend/git-inspection-apps/`, assets
under `src/forgemcp/apps/assets/`, and tests in
`tests/test_mcp_apps_git_inspection.py`.

Finish with the worker report required by the common contract.

