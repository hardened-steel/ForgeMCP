# Implement CMake result Apps

Read `00-common-contract.md` first. Create branch `codex/apps-cmake` from the
common base.

## Tool coverage

Bind all ten tools:

- `cmake__status`
- `cmake__list_kits`
- `cmake__select_kit`
- `cmake__list_build_trees`
- `cmake__list_presets`
- `cmake__configure`
- `cmake__list_targets`
- `cmake__build`
- `cmake__ctest_list_tests`
- `cmake__ctest_run`

## Resources

Use two shared assets:

- status/catalog tools (`status`, `list_kits`, `list_build_trees`,
  `list_presets`, `list_targets`, `ctest_list_tests`) →
  `ui://forgemcp/cmake/catalog`
- operation tools (`select_kit`, `configure`, `build`, `ctest_run`) →
  `ui://forgemcp/cmake/operation`

Suggested App names: `forgemcp-cmake-catalog` and
`forgemcp-cmake-operation`.

## UX

Catalog view uses a compact terminal header and a fixed table/list viewport:

- status: availability, version, configured/stale state and validated
  compilation database state;
- kits: path-free origin/family/driver/ABI/readiness rows;
- build trees: relative build tree, generator, compatibility/adoption state;
- presets: configure preset names and safe metadata;
- targets: target name/type/artifact-relative metadata;
- tests: names and safe test metadata.

Click/focus previews a row in a fixed detail strip. Local filtering is allowed
only when useful for a long list. No Select/Configure/Build/Test buttons.

Operation view presents terminal-like execution outcome, duration, state,
warnings and normalized diagnostics. Build/configure/test failures must look
different from transport/tool failure. Diagnostics are locally selectable and
show their workspace-relative location/message in a fixed detail area. Never
display raw output, argv, environment, executable paths, compiler command or
kit-private data. A successful `select_kit` is only a result confirmation.

Inspect real models in `src/forgemcp/cmake/models.py` and the plugin output
paths. Do not change configure/build semantics or progress.

## Ownership

Edit only the CMake plugin for registrations. Add frontend sources under
`frontend/cmake-apps/`, named assets under `src/forgemcp/apps/assets/`, and
tests in `tests/test_mcp_apps_cmake.py`.

Finish with the worker report required by the common contract.

