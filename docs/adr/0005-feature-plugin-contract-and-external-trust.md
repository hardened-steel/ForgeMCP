# ADR 0005: Use a versioned, transport-neutral feature-plugin contract with opt-in trusted discovery

## Context

ForgeMCP needs optional CMake, clangd, debugger, and future integrations without turning Core into a collection of feature-specific lifecycle and transport code. Those integrations need safe access to the configured workspace and process runtime, can depend on one another, and may offer MCP operations. At the same time, Python entry points execute package import code, which makes indiscriminate plugin discovery an arbitrary-code-execution mechanism.

Workspace and Process Runtime are already required foundations of the server. Treating them as removable feature plugins would make lifecycle and safety policy less predictable.

## Decision

Expose `forgemcp.plugins` as the stable public API. `ForgePlugin` owns immutable `PluginMetadata`, which has a lower-case unique `plugin_id`, a major `PLUGIN_API_VERSION` string, feature-plugin dependencies, required Core-service names, and globally unique capabilities. The current API version is `"1"`; a different version is rejected before any plugin starts.

`PluginManager` is a Core-composed service named `plugins`. ForgeMCP-owned feature integrations must call `register_builtin()` explicitly from application composition; there is no implicit module scanning. It validates duplicate IDs and capabilities at registration, validates missing dependencies and Core services before startup, detects cycles, and starts an ID-sorted topological order. A failure first invokes the failing plugin's idempotent `stop()` so resources or status providers acquired before the exception can be released, then rolls back every successfully started plugin in reverse order. `aclose()` is idempotent and normally stops every started plugin in reverse order. Application startup is asynchronous so plugin startup can be awaited; application shutdown always closes the manager before `ProcessRuntime`.

`PluginContext` intentionally contains no `ForgeApplication` or raw `ServiceRegistry`. It exposes immutable configuration, a structured logger, and only services named in `requires_services`; it also exposes a plugin-scoped tool-registration facade. Plugins must not import or construct `ForgeApplication`.

Tool declarations are transport-neutral `ToolContribution` values. Each carries a local lower-case operation name, description, and mapping-based sync or async handler. `ToolRegistry` derives the stable MCP-safe name `<plugin_id>__<tool_name>` and rejects duplicate names. Legacy handlers remain `handler(arguments)`. A context-aware handler explicitly declares `handler(arguments, *, execution_context: ToolExecutionContext)`; the registry only supplies the named keyword, so optional legacy positional parameters cannot be rebound accidentally. The immutable request-scoped context exposes progress capability/reporting and cancellation checks but no SDK object. Only `server.py` imports FastMCP and adapts registrations after all plugin startup succeeds.

Without changing `PLUGIN_API_VERSION`, Phase C adds optional plugin-scoped
facades for `ResourceContribution`, `ResourceTemplateContribution`,
`PromptContribution`, and `CompletionContribution`. They remain mapping/value
contracts with no FastMCP `Context`, `ServerSession`, or MCP types. Public
resource URIs/templates and prompt names are not implicitly plugin-qualified,
so the application registry rejects duplicates globally. Completion providers
are unique by reference kind, exact prompt/template reference, and argument.
All registries have fixed capacities and lexical snapshot order. The manager
unregisters every contribution kind on partial-start failure, reverse rollback,
and normal stop. Existing external plugins that only access `context.tools` and
register legacy `ToolContribution` handlers remain source-compatible.

External discovery uses the Python entry-point group `forgemcp.plugins`, but it is off by default. It requires both `external_plugins_enabled` and a non-empty explicit allow-list of entry-point names. The manager does not even query entry-point metadata unless both gates pass, and it calls `EntryPoint.load()` only for an allow-listed name. The loaded object must be a `ForgePlugin` instance whose `plugin_id` exactly matches that entry-point name. The allow-list is therefore an explicit grant for a known distribution-provided code path, not a request to load every package that advertises the group.

## Consequences

Feature integrations have a testable dependency and lifecycle contract, while Workspace and Process Runtime stay always present and policy-controlled. Tool adapters remain isolated from MCP SDK types, so a later transport can reuse the same plugins and registry.

Resources and prompts are model-facing authored content. ForgeMCP builtin
control text is trusted code, while filenames, targets, tests, and resource/log
values are untrusted JSON data. An allow-listed external plugin remains trusted
in-process code and can author model-facing instructions through a prompt or
resource; operators must include that injection surface in plugin review.
ForgeMCP enforces sizes, names, duplicate rejection, deadlines for cooperative
async handlers, and deterministic output normalization, but cannot sandbox a
malicious or CPU-blocking in-process plugin.

An allow-listed external plugin is still trusted, in-process Python code. It can execute arbitrary code at import time and can use everything exposed by the Python process outside the limited `PluginContext`; the context is an architectural capability boundary, not an OS sandbox. Operators must review, pin, and control the installation provenance of each allowed distribution. A later requirement for untrusted extensions needs process isolation, an IPC protocol, and a separate security decision; it must not weaken this allow-list gate.

Plugin stop failures are recorded in status and logged by exception class while remaining plugins continue to shut down. The orchestrator must use `await ForgeApplication.aclose()` in asynchronous hosts and should inspect plugin status when startup fails, because a failed startup leaves its successfully started dependencies rolled back and the plugin manager terminal for that application instance.
