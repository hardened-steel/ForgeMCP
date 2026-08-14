# ADR 0010: Quality tools use fixed discovery, snapshot-CAS formatting, and read-only analysis

## Context

ForgeMCP needs useful C/C++ quality workflows without turning the MCP server
into an arbitrary tool runner or granting a client a way to mutate source
implicitly. `clang-format` can write files in place; `clang-tidy` can apply
fixes, load plugins, and consumes a project compilation database whose compiler
arguments affect frontend behavior. Sanitizer output is useful to normalize but
executing an instrumented binary needs its own execution and environment policy.

## Decision

Create the builtin transport-neutral `QualityPlugin` with `ClangFormatService`,
`ClangTidyService`, and `SanitizerReportParser`. It receives only Workspace and
Process Runtime services and registers `quality__status`, `clang_format__check`,
`clang_format__apply`, `clang_tidy__list_checks`, `clang_tidy__run`, and
`sanitizer__parse_report` as ToolContributions. Plugin startup starts no child;
missing executables are represented by quality status rather than an application
startup failure.

Executable selection is not an MCP parameter. ForgeMCP considers explicit
absolute `FORGEMCP_CLANG_FORMAT` and `FORGEMCP_CLANG_TIDY` values first, then
Process Runtime's policy-approved PATH basename and small fixed conventional LLVM
locations. Every candidate is qualified using a bounded fixed `--version` argv
probe through Process Runtime. Exact configured paths retain ProcessPolicy's
canonical path, metadata, symlink/reparse, and replacement checks; basename
discovery remains subject to its normal policy approval.

Formatting accepts only explicitly enumerated workspace-relative C/C++ source
paths. It never accepts globbing, style/config choices, extra arguments, or
`clang-format -i`. Instead it requests `--output-replacements-xml`, verifies
strict XML and non-overlapping UTF-8 byte boundaries, calculates the formatted
SHA-256 in memory, and retains no source/replacement text in results or logs.
Apply requires every client-provided snapshot SHA-256, obtains every structured
format result before a mutation, then invokes one Workspace `apply_text_edits`
batch. A per-file process/parse failure or a detected stale snapshot makes no
workspace edit; detected multi-file CAS conflicts are all-or-nothing. The
underlying Workspace staged commit's documented best-effort rollback remains the
limit for I/O failure, crash/power loss, locks, and a final external-writer race.

clang-tidy is read-only in this phase. It accepts only explicit source files,
one validated generated workspace directory containing `compile_commands.json`,
an optional bounded `--checks=<pattern>` value, and bounded timeout. It does not
publish `--fix`, `--fix-errors`, `--load`, `--extra-arg`,
`--extra-arg-before`, arbitrary `--config`, arbitrary header filters, or generic
arguments. Phase 1 uses a strict compiler-style output parser instead of
`--export-fixes` YAML, avoiding a new parser dependency and ensuring no
replacement can be applied. It supports Windows drive-colon locations and
multiline continuations; external diagnostics are counted and omitted.

The CMake/workspace project and its compile commands are a trust boundary:
clang-tidy may process project-controlled compiler arguments, and ForgeMCP is
not a sandbox. Arbitrary clang-tidy plugin loading is not exposed over MCP.
Raw diagnostics, compiler arguments, source, output, environment values, and
external-file contents never enter logs or public raw-output fields.

Sanitizer scope is parser-only. The parser accepts bounded text, recognizes ASan,
UBSan, and unknown fallback reports, returns bounded workspace-only frames and
opaque addresses, and marks malformed/partial/multiple reports. It launches no
program and fetches no source or symbols. A future `sanitizer__run` requires a
separate binary-execution/environment policy.

## Consequences

The quality feature gives stable bounded status, formatting, diagnostics, and
report parsing while preserving the Process Runtime and Workspace as the only
process/file authority. Intentional Phase 1 limits are no formatting of files
whose structured tool output is truncated, no auto-fix, no generic runner, no
untrusted compilation database sandbox, no sanitizer execution, and no promise
of crash-atomic filesystem transactions.
