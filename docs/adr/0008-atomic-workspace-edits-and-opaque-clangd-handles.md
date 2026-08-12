# ADR 0008: Apply clangd WorkspaceEdits atomically and expose opaque cached handles

## Context

Phase 2 adds clangd rename, code actions, and formatting. LSP represents their
file changes as `WorkspaceEdit`, whose targets and ranges are untrusted server
data. It can also contain resource operations and out-of-workspace URIs that
ForgeMCP cannot safely apply. Code actions and call/type hierarchy methods use
server-specific opaque objects that must not become a raw LSP proxy surface.

## Decision

Extend `WorkspaceService` with `apply_text_edits(edits_by_path,
expected_snapshots)`. It accepts only workspace-relative paths and structured
Unicode-code-point ranges, validates all text in memory, deterministically
orders edits, rejects overlap, checks every expected snapshot, then stages and
commits the whole batch with the existing rollback protocol. It returns only
metadata-only `PatchResult`/`FileChange` data. It never accepts native paths
from clangd, never logs content/replacements, and cannot create, delete, or
rename files.

`ClangdService` is the single LSP WorkspaceEdit adapter. It accepts `changes`
and `documentChanges` only when every entry is `TextDocumentEdit`, revalidates
each URI through WorkspaceService, converts from the negotiated LSP position
encoding, verifies an open-document version when supplied, and snapshots all
targets immediately before atomic commit. Any external URI, resource operation,
malformed edit, overlap, stale document version, or snapshot conflict cancels
the entire edit. On success it synchronizes all changed open documents using
full `didChange`, invalidates cached actions/hierarchy handles, and marks prior
diagnostics stale. Request cancellation and content-modified LSP errors are
separate client-visible clangd errors.

Code actions and call/type hierarchy items receive ForgeMCP-generated opaque
handles. Their raw LSP objects remain in an in-memory cache with 100 entries
per class and a two-minute TTL. The cache clears on stop, crash, and document
change. Applying an action is restricted to a resolved pure WorkspaceEdit;
command-only actions are intentionally rejected and ForgeMCP never sends
`workspace/executeCommand`.

## Consequences

Semantic edits retain WorkspaceService's all-or-nothing and symlink-scoped
guarantees. A multi-file rename cannot partially modify a workspace. Clients
must retry an expired handle or snapshot conflict. Resource operations,
external-source edits, command execution, arbitrary LSP calls, and DAP remain
outside this phase. Later features must reuse this adapter and Workspace
transaction rather than adding feature-specific file writers.
