# ADR 0019: All-tool MCP App result families and no-action UI policy

## Context

ForgeMCP exposes 72 stable model-visible tools across Core, Workspace, CMake,
Quality, Git, clangd, and debugger. The initial Apps work covered only two
status views, while the full tool surface needs a consistent, safe result
presentation without changing any tool contract or adding a second tool
surface for UI actions.

## Decision

Every existing public tool has exactly one `ToolAppBinding` with visibility
`("model", "app")`. Eighteen static `ui://forgemcp/...` package resources are
shared by compatible public result families; the integration-owned acceptance
manifest is the exact 72-to-19 mapping. `server_status` is the sole legacy
non-namespaced tool allowed by `ToolAppBinding`; all other bindings remain
namespace-qualified. The registry has separate bounded limits for resources
and bindings (32 and 128), and validates the complete inventory during server
composition.

Views may only render their attached result and offer local filtering,
selection, or detail expansion in fixed geometry. They do not call tools or
resources, request browser permissions, navigate, store data, poll, update
model context, or provide action buttons. All strings are untrusted and enter
the DOM using `textContent`; malformed or oversized results fail closed.

## Consequences

Apps-capable hosts receive a complete presentation mapping, while ordinary
MCP clients receive identical schemas, annotations, results, structured
content, and textual fallbacks. The shared frontend workflow remains
browser-free and deterministically checks all generated assets. Production
coverage verifies the real SDK composition, exact bindings/resources/CSP,
package inclusion, no-Apps parity, asset freshness, and authored-source safety.
Visual acceptance remains a manual MCP Inspector review.
