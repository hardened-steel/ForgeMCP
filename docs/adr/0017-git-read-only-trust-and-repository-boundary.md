# ADR 0017: Read-only Git intelligence has a fixed process and repository boundary

## Context

Git contains mutating commands, configuration-driven helpers, external diff and
text conversion hooks, optional locks, prompts, linked worktree metadata, and
project strings that must not become model instructions. The Phase 1 goal is
safe local repository intelligence, not a Git command proxy or sandbox.

## Decision

Use one builtin `forgemcp.git` plugin and application-scoped `GitService`.
Its public models carry only workspace-relative paths and bounded, immutable
data. It accepts only six fixed read-only operations: status, staged/unstaged
diff, log, exact full-OID show, text-file blame, and local branch listing.
No revision expression, response file, arbitrary argv, config/credential,
network, ref/index/worktree mutation, or submodule traversal surface exists.

Git selection is CLI, environment, then qualified discovery. Explicit invalid
selection never falls back. A selection is a canonical regular file outside the
workspace without symlink/reparse traversal; ProcessRuntime rechecks captured
metadata at every launch. Git is invoked only by ProcessRuntime with
`shell=False`, a scrubbed non-interactive environment, `--no-optional-locks`,
no pager, fsmonitor disabled, credential helper disabled, lazy fetch disabled,
and fixed diff controls. Patches disable external diff and textconv. Raw output
and all project-controlled Git strings are excluded from logs/status.

The worktree root reported by Git must equal `ForgeConfig.workspace_root`.
The workspace `.git` entry is independently validated before every operational
Git argv. A normal repository needs a regular, non-reparse `.git` directory.
For a linked worktree, the regular non-reparse `.git` file must point to a
canonical admin directory exactly under `<common-git-dir>/worktrees/<name>`;
its `commondir` and reverse `gitdir` pointer must in turn lead back to that
exact workspace `.git` file. Administrative paths remain private. Any malformed
or replaced entry fails closed. A non-empty `objects/info/alternates` file is
also rejected, rather than granting reads from a second object store. This
allows Git's explicitly qualified common object store for linked worktrees but
does not allow arbitrary alternates. Every reported path is independently
validated through WorkspaceService. Protocol parsers fail closed on malformed,
replacement-decoded, contradictory, oversized, or truncated structured data.

## Consequences

Git metadata/patches are untrusted model-facing project data. Git may read
trusted project metadata and is not an operating-system sandbox, but Phase 1
does not perform network work or intentionally mutate Git state. ProjectStatus
uses only bounded cached scalar facts; a successful Workspace mutation
invalidates the cache, while no-op/failed mutations do not. Future write or
remote capability needs a separate ADR and separate process/trust policy.
