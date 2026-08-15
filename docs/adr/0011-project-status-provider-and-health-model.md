# ADR 0011: Project status aggregates bounded cached provider snapshots

## Context

ForgeMCP needs one project-level view across Core, Workspace, Process Runtime,
plugins, CMake, clangd, the debugger, and Quality. Calling existing feature
status operations is not safe for this purpose: some deliberately qualify an
executable with `--version` or `--help`, while other feature operations can read
source, start a protocol server, run a build, or alter a session. A project
overview must remain usable during long-running work and partial component
failure without becoming an implicit refresh or orchestration endpoint.

One `ForgeApplication` already owns exactly one workspace and all feature state.
Adding a second `ProjectSession` would duplicate the lifecycle boundary and
encourage a god object.

## Decision

Keep `ForgeApplication` as the workspace/session boundary. Compose one
application-scoped `ProjectStatusRegistry` and `ProjectStatusService` under the
Core service names `project_status_registry` and `project_status_service`.
There is no global registry and no persistent status history.

A provider implements the transport-neutral contract
`async snapshot_status() -> ComponentStatus` and has one stable lower-case ID.
The registry rejects duplicate IDs, unregisters idempotently, captures providers
in lexical ID order, executes them concurrently, and enforces both a 250 ms
default per-provider timeout and a one-second default aggregate deadline.
Provider failure or invalid/oversized output becomes a fixed safe component
failure category; exception messages and types are not returned. Overlapping
calls use one single-flight snapshot. Cancellation of one caller does not cancel
the shared work, completion is not cached, and the next non-overlapping call
starts a fresh snapshot. Aggregate/provider timeout and registry shutdown cancel
spawned tasks and attempt a bounded 50 ms cleanup join. A coroutine that
suppresses cancellation remains registry-tracked until completion and its
eventual exception is consumed. This bounds cooperative async providers without
claiming that asyncio can pre-empt CPU-blocking or malicious in-process plugin
code. Shutdown closes the registry before feature teardown, so no new provider
call starts after shutdown begins. Unregister affects future snapshots; an
already captured provider call may complete under the same deadlines.

Feature plugins optionally obtain `project_status_registry` through the existing
declaration-scoped `PluginContext.services` facade. The plugin contract shape and
API major version remain unchanged. Builtin feature plugins declare the service,
register their adapter when started, and unregister it before releasing feature
state. An external plugin may do the same, but absence of a provider has no
effect on its lifecycle. Foundational Core, Workspace, Process Runtime, and
Plugin Manager adapters are registered explicitly by application composition.
`ProjectStatusService` imports no concrete feature service.

The sole new MCP operation is the ProjectPlugin `ToolContribution`
`project__status {}`. It accepts no refresh, raw, or detail option. `server.py`
continues only to adapt contributed tools to FastMCP.

Providers may copy only state already retained in memory. They do not run
commands or qualification/version probes; configure, build, test, format, or
analyze; start clangd or a debugger; read/list source files; request DAP/LSP
data; change lifecycle; poll; or watch files. Small bounded metadata caches for
last CMake and Quality operations do not affect operation results. Each
`ComponentStatus.observed_at` records its independent observation time. The
aggregate is explicitly partial and non-transactional, not a consistent
cross-component transaction.

clangd diagnostic counters scan at most 64 cached documents while holding the
document-state lock. Additional documents set a fixed truncation fact/warning
and staleness; status never acquires clangd's mutation lock or performs a
protocol request.

All public status values use strict immutable Pydantic models. Returning a model
instance is not sufficient trust: the registry dumps and strictly revalidates
every `ComponentStatus`, then checks its ID and observation time. Constructed
models cannot bypass unknown-field, enum, collection, scalar, uniqueness, or
timestamp rules. Observations more than five seconds ahead are invalid;
observations over 24 hours old are invalid; otherwise an age over five minutes
forces `stale=true`. Provider-declared staleness may additionally represent
stale underlying cached domain data. Facts are named string/integer/boolean
scalars only. Strings and collections are bounded and
unknown fields are forbidden. There is no raw/custom JSON payload. Status omits
source/file/patch content, diagnostic messages, compiler/debugger/tool output,
argv, environment, PIDs, variables, stack frames, expressions/evaluate results,
sanitizer symbols, raw exceptions, external plugin module paths, and executable
paths. Workspace/build/compilation-database directories are workspace-relative;
`ProjectStatus.workspace_root` is the fixed marker `configured`, never an
absolute host path.

Registry capacity is 64 providers. A component has at most 128 capabilities, 32
facts, and 32 warnings; aggregate capabilities have a 128-item limit and
aggregate warnings a 32-item limit. Signed integer facts are limited to 64-bit
range. The final JSON is limited to 100,000 UTF-8 bytes. If otherwise-valid
components exceed it, reverse-lexical components are deterministically omitted
and `response_truncated`, `omitted_components`, and `partial` describe the loss.
`failed_components` and `timed_out_components` retain bounded sorted IDs.

Health is deterministic and separate from activity:

- `FAILED` when Core, Workspace, Process Runtime, or Plugin Manager reports
  `FAILED`, is absent, or its provider fails validation/execution;
- otherwise `DEGRADED` for a failed/timed-out/invalid provider, any optional
  component in `FAILED`/`DEGRADED`, a failed clangd/debugger session, plugin
  startup failure, or an explicitly configured capability observed unavailable;
- otherwise `HEALTHY`. An optional tool that has not been configured or
  qualified does not degrade health. A failed build/test/format/tidy is last
  project-operation metadata and a warning, not a ForgeMCP service failure.

Activity is `PAUSED` when the debugger is paused; otherwise `BUSY` while
CMake/Quality work, debugger execution/start/termination, or clangd startup is
active; otherwise `IDLE`. Health and activity do not imply one another.

Builtin provider IDs are `core`, `workspace`, `process_runtime`,
`plugin_manager`, `cmake`, `clangd`, `debugger`, and `quality`.

## Consequences

Clients get one bounded side-effect-free cached overview that remains useful
when optional LLVM tools are unavailable or one provider fails. Concurrent
mutating operations take only their existing locks; status providers use short
state-lock copies where needed and never hold a mutation lock for external work.
Several concurrent status calls share one immutable in-flight result and are
bounded. The result remains a partial, non-transactional collection of
independently observed cached states, never a cross-service transaction.

The view can be slightly inconsistent and cached observations can be unknown or
stale; those conditions are explicit. Phase 1 deliberately has no Git status or
history, aggregated diagnostic messages, refresh operation, raw output/logs,
background polling, file watchers, multi-workspace state, or persistence across
restarts. Git requires its own freshness, ignore, nesting, and trust decision.
