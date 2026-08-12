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
targets immediately before commit. `null` and empty text-edit lists are valid
no-ops; a non-empty request is limited to 100 files, 1,000 text edits, and 1
MiB of UTF-8 replacement text. Any external URI, resource operation, malformed
edit, overlap, stale document version, or snapshot conflict cancels the entire
edit. A delayed rename or format response must still match its request-time
anchor snapshot at the serialized commit boundary. On success it synchronizes
all changed open documents using full `didChange`, invalidates cached
actions/hierarchy handles, and marks prior diagnostics stale. Request
cancellation and content-modified LSP errors are separate client-visible clangd
errors.

Code actions and call/type hierarchy items receive ForgeMCP-generated opaque
handles. Their raw LSP objects remain in an in-memory cache with 100 entries
per class, a 64 KiB maximum per cached payload, and a two-minute monotonic-clock
TTL. Expired entries are purged first and capacity then evicts the oldest entry
(FIFO). The cache clears on stop, crash, and document change. Applying an
action is restricted to a resolved pure WorkspaceEdit and rechecks that its
handle and anchor snapshot survived resolution; command-only actions are
intentionally rejected and ForgeMCP never sends `workspace/executeCommand`.

## Consequences

For detected validation and snapshot conflicts, semantic edits are all-or-nothing
and symlink-scoped: no target is changed. During ordinary I/O failure, the
staged commit attempts best-effort rollback of earlier replacements. This is not
a crash-atomic multi-file filesystem transaction: rollback can fail, including
for a Windows-locked file; a crash or power loss can interrupt it; and another
process can race between the final CAS check and `os.replace`. Clients must
retry an expired handle or snapshot conflict and treat commit errors as an
indeterminate I/O outcome. Resource operations, external-source edits, command
execution, arbitrary LSP calls, and DAP remain outside this phase. Later
features must reuse this adapter and Workspace transaction rather than adding
feature-specific file writers.
