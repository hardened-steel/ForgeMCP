# ADR 0003: Compose a guarded Workspace service with strict unified patches

## Context

ForgeMCP needs a single safe filesystem capability before adding MCP tool adapters, CMake, or language-server integrations. The existing immutable `FileSnapshot`, `FileChange`, and `PatchResult` models already describe content-free state and atomic patch effects, but Core must compose a service under the stable `workspace` name.

The service must not inspect paths outside the configured workspace, follow symlinks into arbitrary locations, or leak source content and patch text to logs. It also needs an optimistic concurrency contract so an assistant cannot overwrite a user edit made after it read a file.

## Decision

Create `forgemcp.workspace.WorkspaceService` and compose it in `ForgeApplication.create()` as `application.services["workspace"]`. Core constructs the service from its already validated `ForgeConfig` and `StructuredLogger`; all filesystem logic, path policy, and errors remain in the Workspace module.

Use a small in-repository parser for a strict, text-only subset of unified diff rather than adding a patch library. It accepts ordinary `---`/`+++` headers and `@@` hunks, supports create, modify, and delete through `/dev/null`, and rejects binary diffs, renames, malformed counts, and no-final-newline markers. Every affected path needs an expected `FileSnapshot`, its SHA-256 digest, or `None` for an expected absent creation target. Hunk and snapshot conflicts return `PatchResult(applied=False)` with no reported changes; invalid patch input raises a Workspace domain error.

Treat symlinks as forbidden, rather than resolving and then allowing a target that happens to fall under the root. Any symlink component in a caller-requested path is rejected. Directory listing uses non-following scans and omits symlink entries. The default configurable policy excludes `.git`, `.venv`, `build`, `build-*`, and `cmake-build-*`; it also bounds text reads and patch bytes.

Patch bytes are parsed and applied in memory, replacement files are staged in each target directory, and existing files are first moved to same-directory rollback backups. If a replacement fails, already committed targets are restored. Neither file contents nor patch text is emitted to the logger.

## Consequences

The application status now reports the `workspace` service, while Core still has no Workspace business logic or MCP tool registration. There are no new runtime dependencies.

Unified diff support is deliberately conservative. Clients needing renames, binary changes, or exact preservation of a missing final newline need a later format extension and ADR.

Expected snapshots provide optimistic concurrency, not cross-process filesystem locking. ForgeMCP checks snapshots before staging and immediately before commit, but an external writer with filesystem access can still race OS-level replacement operations. This is the remaining orchestrator risk; callers should retry after `PatchResult(applied=False)` or a patch commit error.
