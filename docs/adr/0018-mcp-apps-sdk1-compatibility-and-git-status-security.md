# ADR 0018: MCP Apps SDK 1.x compatibility and Git Status App security

## Context

ForgeMCP's Git Intelligence Phase 1 already exposes `git__status` as a
read-only, bounded tool with a strict output schema, structured content, and
textual fallback. Phase 1 Apps must add an interactive status view without
adding a model-visible mutation tool, leaking repository internals, or forcing
the application to migrate from MCP Python SDK 1.x.

The stable MCP Apps extension `2026-01-26` uses
`io.modelcontextprotocol/ui`, static `ui://` resources with MIME
`text/html;profile=mcp-app`, nested `_meta.ui.resourceUri`, and the standard
MCP extension capability map. SDK 1.x preserves generic Tool and Resource
`_meta`, but does not yet model `capabilities.extensions` or offer Apps
registration classes.

## Decision

Feature plugins receive only transport-neutral `AppResourceContribution`,
`ToolAppBinding`, `AppCsp`, and a plugin-scoped App registry facade. Resources
have a unique static `ui://` URI, bounded immutable HTML, explicit CSP/domain/
permission/preferred-border metadata, and plugin ownership. Bindings have one
existing public tool, one registered App URI, and explicit `model`/`app`
visibility. The application registry validates missing/duplicate tool and
resource references after plugin startup, participates in failed-start rollback,
and clears during normal shutdown. GitPlugin registers only:

```json
{
  "tool": "git__status",
  "resource": "ui://forgemcp/git/status",
  "mimeType": "text/html;profile=mcp-app",
  "visibility": ["model", "app"]
}
```

`server.py` is the sole SDK 1.x compatibility boundary. It passes public
generic `meta` arguments to FastMCP for the nested Tool and Resource metadata.
The supported floor is `mcp>=1.29,<2`: this is the first ForgeMCP-tested 1.x
floor with those public decorators and generic `_meta` models. A regression
also pins the deliberately narrow SDK 1.x `ServerCapabilities` extra-field
contract used only for the missing typed extensions field.

One documented, bounded initialization adapter injects the wire-only server
capability `{ "extensions": { "io.modelcontextprotocol/ui": {} } }` and
continues to omit empty `experimental`. A second read-only SDK 1.x helper reads
the current connection's initialize parameters only while handling `tools/list`;
it attaches App metadata solely when the client declares the exact App MIME
type. It retains no cross-connection mutable capability state. On SDK 2.x
migration, remove `_install_sdk1_apps_compatibility_adapter`,
`_connection_supports_mcp_apps`, and the `model_extra` fallback in
`client_supports_mcp_apps`; replace them with typed server-extension and
current-connection client-capability APIs. Keep the public
`FastMCP.tool(..., meta=...)` and `FastMCP.resource(..., meta=...)`
projections: those are ordinary wire metadata, not the temporary shim.

All clients retain the same `git__status` name, schema, annotations,
read-only behavior, structured content, and JSON-text fallback. Apps-capable
clients receive the binding; clients without the exact MIME receive the plain
tool definition and never need to act on UI resources. The `ui://` resource is
still statically registered so a capable host can inspect/fetch it normally.

The production asset is a package resource loaded through `importlib.resources`
at plugin startup. It is static, HTML5, UTF-8 and byte-bounded; missing or
corrupt assets stop Git plugin composition rather than falling back to a
workspace path. Node is development-only. `frontend/git-status` has source,
lock file, an offline `npm run build`, and source-digest verification. The
single-file view uses direct JSON-RPC over `postMessage`, a deliberately narrow
implementation of `ui/initialize`, initialized, tool input/result/cancel,
host-context, size, and teardown lifecycle messages. The direct implementation
is safer for this no-dependency App than adding an unbundled runtime or CDN:
its protocol surface is small, static, protocol-tested, and contains no
network, host-resource read, arbitrary tool, or browser permission API.

The Git Status App has restrictive CSP metadata with all four domain lists
explicitly empty, no permissions, no dedicated domain, and `prefersBorder=true`.
It calls only `git__status` for Refresh, allows one active request, keeps the
last successful state after failure, and never updates model context. Project
strings are rendered through DOM construction and `textContent`; unsafe HTML
sinks, template concatenation, external assets, browser storage, eval,
networking, nested frames, clipboard, and host DOM access are absent.

## Consequences

The 72-tool model surface remains stable while Apps-capable hosts can render a
safe interactive Git dashboard. The same result remains meaningful in every
non-Apps host. The UI cannot stage, commit, checkout, read arbitrary resources,
or call arbitrary tools. Maintaining the narrow SDK boundary is temporary
technical debt, protected by live SDK stdio Apps/no-Apps regression tests and
Inspector App-info validation.
