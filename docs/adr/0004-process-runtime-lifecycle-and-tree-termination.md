# ADR 0004: Isolate external tool process groups and separate captured commands from protocol handles

## Context

CMake and CTest are ordinarily short, terminal commands: callers need a bounded result with stdout and stderr available for diagnostics. clangd and Debug Adapter Protocol adapters instead hold a bidirectional session for an unbounded period and need streaming access to stdin, stdout, and stderr. Treating both cases as a buffered command result would either deadlock a protocol session or discard its framing.

The runtime must execute tools safely from a configured workspace without providing a general shell capability. It must also terminate descendants when an operation times out, is cancelled, or the service shuts down. That needs platform-specific mechanics: POSIX has process groups and signals; Windows has console process groups and `taskkill` tree termination.

## Decision

Create `forgemcp.processes` as a transport-neutral async service and compose one `ProcessRuntime` in Core as `application.services["process_runtime"]`. The module exposes two deliberate APIs:

- `ProcessRuntime.run(argv, ...)` launches a short command with `asyncio.create_subprocess_exec(..., shell=False)`, captures stdout and stderr in separate incremental UTF-8 collectors, and returns `ProcessResult`. Each collector has the active `ProcessPolicy.max_output_characters` bound, which cannot exceed the `ProcessOutput` contract maximum. It preserves output whitespace and marks discarded capture data as truncated.
- `ProcessRuntime.start(argv, ...)` launches a protocol child and returns `ProcessHandle`, whose asyncio stdin/stdout/stderr streams are owned by the protocol adapter. It does not produce a buffered `ProcessResult`. The adapter must call `aclose()` when finished; runtime shutdown awaits `ProcessRuntime.aclose()` to stop all remaining handles.

Commands are always NUL-free argv values, never shell strings, and there is no generic MCP `run_shell` operation. A policy allow-lists bare executable names and/or resolved absolute executable paths. A bare name is resolved from the environment captured when the runtime is composed, before per-launch environment overrides are merged, so an override cannot replace the selected executable through `PATH`. CWD must be an existing, non-symlink workspace-relative directory and may be further allow-listed. The policy also bounds timeouts, output, graceful termination time, and environment inheritance/override keys. argv, environment values, and raw output are never placed in logs or returned in an error.

Every POSIX child is started in a new session/process group. Graceful cleanup sends `SIGTERM` to that group and, after the grace period, sends `SIGKILL`. Every Windows child has `CREATE_NEW_PROCESS_GROUP` and is assigned to a private Job Object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, using only the standard-library `ctypes` bindings. Graceful cleanup attempts `CTRL_BREAK_EVENT` and falls back to direct termination; closing the Job Object is the forced tree kill and also removes descendants when a direct child exits first. If a host-owned Job forbids assignment, forced cleanup falls back to Windows' built-in `taskkill /PID <pid> /T /F` with `asyncio.create_subprocess_exec` and discarded streams, then reaps the direct child. No shell is used for the helper either.

Do not add `psutil`. POSIX process groups and the standard Windows Job Object provide the normal tree-cleanup path without a runtime dependency; `taskkill` is a fallback for hosts that do not permit Job assignment. A future requirement to enumerate or control already-detached Windows descendants would justify a dependency or a new native integration, with a new ADR.

## Consequences

Short commands return stable, bounded, serializable models; protocol clients retain the lossless streaming control their wire formats require. Timeout returns a `ProcessResult` with `timed_out=True` and no exit code. Cancellation propagates after cleanup rather than becoming a result value.

The runtime cannot protect against a child that intentionally detaches into a new POSIX session or Windows process group, or requests Windows Job breakaway. Tool adapters must not request detachment. On Windows, `CTRL_BREAK_EVENT` also depends on console delivery; Job closure supplies forced tree termination, with `taskkill` as a fallback while the parent process identifier remains available. The canonical lifecycle is asynchronous `await ForgeApplication.aclose()` (or, for an independently composed runtime, `await ProcessRuntime.aclose()`) before the host event loop is destroyed. The stdio MCP adapter now does this through FastMCP's lifespan `finally`, including after a transport exception. Core's synchronous `stop()` remains an immediate best-effort fallback for other synchronous hosts.
