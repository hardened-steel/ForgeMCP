# ADR 0015: Bound MCP discovery, logging, and model-facing authored content

## Context

The tool-only stdio surface makes safe workflows difficult to discover and
long-lived operations difficult to observe. MCP `2025-11-25` can initialize
with server-wide instructions and advertise resources, prompts, logging, and
completion, but the installed Python SDK 1.x must remain the wire-protocol
owner. Passing FastMCP contexts or sessions into CMake, Workspace, Project, or
external feature services would break the existing transport-neutral boundary.

Resources and prompts also create a model-facing injection boundary. ForgeMCP
must distinguish its authored control text from project-controlled filenames,
targets, tests, and log metadata. Operational logging cannot be implemented by
parsing stderr because that loses types and risks forwarding values that were
never approved for model-facing use.

## Decision

Keep `server.py` as the sole MCP SDK adapter. Extend plugin API version 1 with
bounded application-owned resource, template, prompt, and completion
contribution registries as described by ADR 0005. No feature handler receives a
FastMCP decorator, `Context`, `ServerSession`, MCP request, or wire type.
Registrations are deterministic and duplicate public keys fail plugin startup;
all contribution kinds participate in partial-start cleanup and reverse
lifecycle rollback. There is no global registry.

Initialize as implementation `ForgeMCP` with the installed `forgemcp` package
metadata version. Supply a static 904-byte instruction value authored only in
ForgeMCP code. The first 512 characters independently state the workspace,
status-first, Workspace/CMake preference, clangd/debugger/Quality roles,
snapshot/CAS, trusted-code, and untrusted-data rules. The remainder gives the
normal status → configure-if-needed → compilation database → clangd → build/
test → report workflow. Instructions contain no discovered state and are MCP
server guidance whose application depends on the host, not a system-role
message.

Advertise Tools, Resources, Prompts, Logging, and Completions only because all
five request handlers are installed. Set empty Experimental to absent and leave
Tasks absent. Continue to negotiate the SDK-supported legacy `2025-11-25`
stdio protocol; do not implement modern frames or custom capability messages.

Expose versioned bounded `application/json` at:

- `forgemcp://about`;
- `forgemcp://project/status`;
- `forgemcp://workspace/files`;
- `forgemcp://cmake/targets`; and
- `forgemcp://logs/recent`.

The templates are `forgemcp://workspace/files/{cursor}`,
`forgemcp://cmake/targets/{profile}`, and
`forgemcp://logs/recent/{level}/{limit}`. Resource output is limited to 256 KiB
and eight concurrent reads. The manifest retains a deterministic prefix of at
most 1,000 safe files and pages 50 entries. Random 32-character cursors live in
one 32-entry application cache for five minutes, bind to the application and
Workspace mutation generation, and reveal no path, offset, or generation.
External changes can still race the walk, so `transactional_snapshot=false`.

Phase D1 adds cached path-free `forgemcp://cmake/kits` and
`forgemcp://cmake/kits/{kit}`. These resource/completion reads never refresh
discovery, execute a compiler or CMake process, or disclose private toolchain
paths or environment values.

CMake caches only already validated File API target models under random opaque
profile IDs for ten minutes, at most 16 profiles. A resource read never
configures, creates a File API query, or invokes a subprocess. It emits a fixed
unavailable/stale/available state and bounded target name/type plus validated
relative artifacts. Preset completion may perform the existing bounded preset
file read; target/configuration/profile/test completion uses cache only and
never invokes CMake or CTest.

Add five fixed workflow prompts: `forgemcp_build_report`,
`forgemcp_test_report`, `forgemcp_diagnose_build`,
`forgemcp_analyze_file`, and `forgemcp_debug_target`. Their handlers return
messages only. Identifiers have strict name/count/length/control rules and are
placed in a separate JSON-labeled untrusted-data message. Unknown arguments
fail. Completion providers cover every meaningful prompt/template argument,
validate at most 16 context arguments, apply deterministic prefix filtering and
deduplication, return at most 100 values, and populate `total`/`hasMore`.
Legacy completion is deliberately limited to `PromptReference` and
`ResourceTemplateReference`; arbitrary tool JSON arguments are not claimed.

Replace the shared named Python logger with one application-owned fan-out.
ForgeMCP creates one immutable event with a monotonic sequence, UTC timestamp,
one of eight MCP levels, fixed logger/category, and allow-listed bounded scalar
metadata. The same value goes directly to:

1. an independently thresholded JSON stderr sink controlled by
   `FORGEMCP_LOG_LEVEL`;
2. a 256-event/512-KiB deterministic recent ring; and
3. an optional connection sink controlled only by `logging/setLevel`.

The connection sink has a 64-event queue, one worker/active send, a 20 Hz
ceiling, and a 500 ms delivery deadline. Queue saturation drops notifications;
slow/disconnected delivery disables itself. Cancellation-resistant sends are
detached once, cancelled, and have their outcome consumed; they do not delay
tool execution or application shutdown. Registering a sink does not replay the
ring. Disconnect/application shutdown unregisters and closes it. Progress is
not logged. The log resource reads structured events directly, emits no read
event, and never parses stderr.

All sinks reject source/file content, patch/edit text, raw subprocess output,
argv/environment, absolute paths, compile commands, LSP/DAP payloads,
diagnostic text, raw exception messages, PIDs/handles, and secret-like values.
Unknown log categories collapse to one fixed category and unknown metadata keys
are omitted. The ring is cleared at application shutdown.

ForgeMCP-authored instructions and fixed prompt workflow text are trusted code.
Project-controlled strings are untrusted data and remain JSON values. External
plugins are trusted in-process code under the existing explicit allow-list, but
their authored resource/prompt content is also a model-facing injection surface
that operators must review. Size/schema normalization is not an OS or model
sandbox.

The SDK capability builder reports resource subscriptions as unsupported.
Phase C therefore advertises `subscribe=false` and implements no resource
change notifications. Workspace mutation, successful configure, and Project
status remain observable on the next read. A later subscription design requires
SDK support and its own connection/coalescing lifecycle; no ad-hoc transport is
added here.

## Consequences

Codex and other capable clients can discover safe workflows and cached project
state without running tools, while clients that ignore resources/prompts still
retain the complete legacy tool surface. MCP logging cannot block tool work and
has a smaller disclosure surface than stderr parsing. Application isolation,
startup rollback, and old `ToolContribution` compatibility remain testable.

The manifest is not a filesystem transaction, cached CMake/tests can be absent
until the corresponding ordinary operation populates them, and notification
loss under flood or slow clients is intentional. Cooperative deadlines cannot
pre-empt CPU-blocking trusted Python plugin code. Tasks, modern protocol-era
features, Git, filesystem watching, delete/rename, binary writes, Apps/UI, and
resource subscriptions remain out of scope.
