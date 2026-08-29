# ADR 0013: Keep MCP progress request-scoped behind a transport-neutral execution context

## Context

Long configure/build/test work appears stalled in MCP clients even though ForgeMCP
already bounds subprocess output, timeouts, cancellation, and process-tree
cleanup. Passing FastMCP `Context` into CMake, Quality, clangd, or debugger
would leak transport/session lifetime into application services and make
external plugins transport-dependent. Parsing or returning raw compiler output
would expand the disclosure surface.

## Decision

Expose `ToolExecutionContext`, `ProgressReporter`, `NoOpProgressReporter`, and
immutable `ProgressUpdate` from the public plugin contract. A handler may opt
in with named `execution_context`; old mapping-only handlers continue exactly
as before. Contexts are per call, cannot be retained by Core/application
services, contain no FastMCP/ServerSession types, and use normal asyncio
cancellation. `server.py` creates a fresh reporter only when an SDK request has
a progress token and sends `ctx.report_progress(progress, total, message)`.
String, numeric, and numeric-zero tokens are preserved only in that bridge;
missing token/capability, slow delivery, and non-cancellation transport errors
disable progress for that call without changing the tool result. Delivery is
synchronous, serialized, rate-limited, bounded by a short notification timeout,
and keeps at most one detached cancellation-resistant send for a disabled call;
its outcome is consumed and it cannot create a notification flood.

The context normalizes every notification monotonically across phase,
heartbeat, and exact measurements. A terminal success may advance a known
exact total to `total/total`; failure, timeout, and cancellation preserve the
last real value. A contextual handler must explicitly declare the keyword-only
`execution_context`; every other handler remains the legacy one-argument form.

`ProcessRuntime.run` gains an optional local bounded output observer. It uses
separate pipe drainers plus one bounded observer queue/worker. Overflow drops
events and produces safe metadata; failures are isolated. The observer is not a
protocol stream, does not change DAP/LSP/start stdin behavior, and never emits
raw text itself. Incremental decoding is independent per stream and a bounded
optional observer finalizer can flush an unterminated line. CMake is the first
consumer and derives only fixed phases, two-second heartbeats, strict Ninja
exact progress, and strict CTest progress; output-derived CTest names are never
sent to clients.

## Consequences

Progress is best effort and client-dependent, but operations retain Phase-A
timeouts and cancellation/tree-cleanup semantics. Success has a terminal update;
failure, timeout, and cancellation never claim false completion. No resources,
prompts, completion, Git integration, arbitrary CTest options/regex, or raw
compiler-output MCP API is introduced. Exact percentage remains generator and
format dependent; MSBuild and unknown output use activity heartbeats.

D2.4 exercises numeric-zero and string tokens, no-token calls, request
isolation, exact/heartbeat parsing, slow/failing transport, and
timeout/cancellation through SDK stdio. This is acceptance evidence only:
progress cannot extend a deadline or change a tool result.
