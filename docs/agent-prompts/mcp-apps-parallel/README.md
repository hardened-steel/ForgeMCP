# Parallel MCP Apps implementation prompts

These prompts cover the 70 public tools which do not yet have an MCP App. The
existing `git__status` and `project__status` Apps are out of scope.

Run the six implementation prompts in parallel, each from the same current
`main` commit and in its own branch/worktree:

1. `01-core-workspace.md` → `codex/apps-core-workspace`
2. `02-cmake.md` → `codex/apps-cmake`
3. `03-quality.md` → `codex/apps-quality`
4. `04-git-inspection.md` → `codex/apps-git-inspection`
5. `05-clangd.md` → `codex/apps-clangd`
6. `06-debugger.md` → `codex/apps-debugger`

After all six branches are complete, give `99-integration.md` to a separate
integrator. Do not run the integration prompt concurrently with implementation.

The design intentionally shares one UI resource for closely related result
shapes instead of bundling the official MCP Apps runtime once per tool. Every
tool still receives an explicit App binding. This keeps the wheel and frontend
surface bounded while retaining tool-specific result rendering.

All workers must read `00-common-contract.md` before their subsystem prompt.

## Coverage

| Prompt | Tools |
| --- | ---: |
| Core / Workspace | 6 |
| CMake | 10 |
| Quality | 6 |
| Git inspection | 5 |
| clangd | 27 |
| Debugger | 16 |
| Total new bindings | 70 |

Together with the two existing status Apps, integration should produce 72/72
model-visible tools with an Apps-capable presentation and still exactly 72
model-visible tools.

