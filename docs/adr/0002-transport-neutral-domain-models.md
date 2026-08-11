# ADR 0002: Use immutable Pydantic domain models with UTC timestamps and bounded output

## Context

Workspace operations, process execution, CMake, clangd, and debugging will exchange the same kinds of locations, diagnostics, task outcomes, process results, and file-change reports. Passing MCP SDK, CMake, LSP, DAP, `Path`, or subprocess objects across those boundaries would couple services to a particular adapter and make their outputs difficult to validate and serialize consistently.

File edits and process captures may also contain source code or secrets. They need distinct treatment in observability: patch reports are useful structured log data, while raw process output is not safe to log indiscriminately.

## Decision

Create `forgemcp.models` as an independent Pydantic v2 package of immutable value objects. It forbids unknown fields, uses only JSON-compatible primitives, enums, nested models, and timezone-aware datetimes, and documents every public field with Pydantic schema descriptions.

All timestamp fields are `datetime` values normalized to UTC; naive input is rejected. JSON serialization uses ISO-8601 UTC strings. Source coordinates use zero-based line and Unicode-code-point column positions, and ranges are half-open.

`ProcessOutput` caps each stdout or stderr payload at 65,536 Unicode code points. Capture implementations must set `truncated=True` when they discard excess data. `ProcessOutput.log_summary()` exposes only output length and truncation status for structured logging.

`FileSnapshot`, `FileChange`, and `PatchResult` contain file metadata and hashes only. They intentionally have no file-content, patch-text, or free-form text fields. `PatchResult` represents an atomic patch: a failed result cannot report changes.

## Consequences

Future adapters must translate their native types at their edges and must truncate output before constructing `ProcessOutput`. They gain a stable validation and JSON contract, as well as file-change records that are safe to log. A workflow requiring partial patch application needs a separate result model or an explicit ADR amendment; it must not overload `PatchResult`'s atomic semantics.
