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
Missing token/capability, slow delivery, and non-cancellation transport errors
disable progress for that call without changing the tool result. Delivery is
synchronous, serialized, rate-limited, bounded by a short notification timeout,
and creates no unbounded tasks.

`ProcessRuntime.run` gains an optional local bounded output observer. It uses
separate pipe drainers plus one bounded observer queue/worker. Overflow drops
events and produces safe metadata; failures are isolated. The observer is not a
protocol stream, does not change DAP/LSP/start stdin behavior, and never emits
raw text itself. CMake is the first consumer and derives only fixed phases,
two-second heartbeats, strict Ninja exact progress, and strict CTest progress.

## Consequences

Progress is best effort and client-dependent, but operations retain Phase-A
timeouts and cancellation/tree-cleanup semantics. Success has a terminal update;
failure, timeout, and cancellation never claim false completion. No resources,
prompts, completion, Git integration, arbitrary CTest options/regex, or raw
compiler-output MCP API is introduced. Exact percentage remains generator and
format dependent; MSBuild and unknown output use activity heartbeats.
