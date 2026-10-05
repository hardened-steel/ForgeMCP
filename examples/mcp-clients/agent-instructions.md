## Required use of ForgeMCP

For operations on the configured project and service storage, use ForgeMCP
tools and MCP resources exclusively whenever they support the operation.
This includes files and directories, search, toolset inspection, CMake
configuration and builds, CTest runs, process state and logs, and C/C++
diagnostics and semantic analysis.

Select tools using their current descriptions, schemas, and server instructions.
If tools are loaded on demand, discover the relevant ForgeMCP tools first.
A tool missing from the initial list is not evidence that it is unavailable.

Do not substitute a terminal, native file tools, scripts, another MCP server,
or direct CMake, CTest, compiler, or clangd commands for supported operations.
Convenience, familiarity, speed, and needing multiple calls are not reasons
to bypass ForgeMCP.

Use semantic analysis for questions about symbols, definitions, and references;
use text search for textual matches. Follow server instructions, operation
prerequisites, and operator-defined profiles. Retrieve logs of commands launched
by ForgeMCP through ForgeMCP.

Use an alternative only for a specific operation when one of these conditions
has been established:

- The required functionality is unsupported and cannot be achieved by combining
  available ForgeMCP tools or resources.
- The server or required tool is demonstrably unavailable.
- A valid call exposes a tool malfunction or an unavailable dependency that
  cannot be resolved within the current task's authorized scope.

Before switching, check tool descriptions and prerequisites. Correct invalid
arguments and perform required preparation through ForgeMCP; retry when the
cause is recoverable. Compiler errors, failing tests, empty search results,
and project diagnostics do not themselves indicate a tool malfunction.

Before using an alternative, briefly state the blocked operation, the evidence
for the exception, and the alternative you will use. Do not claim unavailability
without checking, or missing functionality without inspecting available capabilities.

The exception applies only to the blocked operation. Continue using ForgeMCP
for all other supported operations, and return to it when the cause disappears.
An alternative does not authorize bypassing access denials, declined approvals,
protected paths, or operator-defined restrictions.
