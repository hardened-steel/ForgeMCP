# ADR 0007: Use one managed LSP session with snapshot synchronization and workspace-only URI projection

## Context

clangd is a long-lived JSON-RPC/LSP server. It needs source text and compiler
database information, but ForgeMCP must not turn its MCP endpoint into an
arbitrary LSP or process proxy. LSP also defines `character` in a negotiated
encoding, while ForgeMCP's public `Position.column` is a Unicode code-point
column. Finally, clangd can return headers, libraries, generated files, and
other URIs outside the configured workspace, which are not safe Workspace
resources to expose as though ForgeMCP could read them.

## Decision

Create a reusable, transport-neutral `forgemcp.lsp` module. `LspClient` owns
JSON-RPC 2.0 Content-Length framing, a single reader task, a unique increasing
numeric request-ID table, out-of-order response matching, bounded message
input, timeout and cancellation through `$/cancelRequest`, and failure of all
pending requests after malformed wire data or EOF. It accepts only byte streams
and has no MCP or Core imports. It responds to the minimal server-to-client
requests necessary for clangd, denies `workspace/applyEdit`, and is not an
arbitrary LSP proxy.

Ship `ClangdPlugin` as an explicitly composed builtin plugin with capability
`clangd`, declaring only `workspace` and `process_runtime`. The plugin creates
an application-owned `ClangdService`, but does not start a child at application
startup. `clangd__start` alone validates a workspace-contained non-symlink
`compile_commands_dir` containing `compile_commands.json`, launches fixed
clangd arguments through Process Runtime, performs `initialize` then
`initialized`, and records the negotiated position encoding. The executable is
the policy-allowed explicit absolute `FORGEMCP_CLANGD` value, if configured, or
the policy-allowed bare `clangd` found using Process Runtime's captured PATH.
There is no caller-provided argv and no `--query-driver` in phase 1.

On shutdown ClangdService sends `didClose` for every known document, requests
`shutdown`, sends `exit`, closes the protocol, and waits before ProcessHandle
termination/kill escalation. It continuously drains stderr with a fixed
discard limit and never emits raw stderr. Repeated close is idempotent. An EOF,
protocol failure, or unexpected child exit moves the service to `failed`; phase
1 has no restart loop.

Documents are identified by WorkspaceService snapshots. ForgeMCP reads text
only through WorkspaceService and sends `didOpen` once per session. A changed
SHA-256 sends a whole-document `didChange` with a monotonic version. Only path,
URI, snapshot, version, and normalized diagnostics persist; source text does
not. Diagnostics are current only when their `publishDiagnostics` notification
matches the active document version/snapshot. The diagnostic result explicitly
reports completeness, timeout, and staleness; a current empty array is success.

Public `Position.column` remains zero-based Unicode code points. The LSP
adapter alone converts to and from the negotiated `utf-8`, `utf-16`, or
`utf-32` encoding and rejects indices that split encoded characters. All MCP
input paths are workspace-relative. Incoming `file:` URIs are percent-decoded
then passed through WorkspaceService's reported-path validation. Results whose
URIs cannot be proved workspace-contained are omitted, and each response gives
an `omitted_external_results` count rather than exposing an external path or
pretending it is a workspace file.

## Consequences

Phase 1 safely provides status, explicit start/stop, diagnostics, hover,
definition, references, document symbols, and workspace symbols as normalized,
bounded ToolContributions. It does not provide rename, WorkspaceEdit
application, code actions, completion, signature help, formatting, hierarchy,
semantic tokens, inlay hints, external file contents, arbitrary LSP calls, or
arbitrary clangd flags.

Phase 2 must extend these same `LspClient`, ClangdService, snapshot, coordinate,
and URI policies. Introducing a second incompatible LSP client or exposing a
raw LSP passthrough would violate this decision.
