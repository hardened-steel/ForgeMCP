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
an absolute-directory-only PATH search and small fixed conventional LLVM
locations. Empty/relative PATH entries, Windows current-directory search, and
workspace-contained candidates are excluded. Every candidate becomes a
canonical regular non-link exact ProcessPolicy approval with captured metadata,
is qualified using bounded fixed `--version` and tool-specific `--help` probes,
and is launched thereafter only by that canonical approved path. Replacement
after approval is rejected. A basename is never the post-qualification launch
authority.

Formatting accepts only explicitly enumerated workspace-relative C/C++ source
paths. It never accepts globbing, style/config choices, extra arguments, or
`clang-format -i`. Instead it supplies the captured source snapshot on bounded
stdin, uses only its validated path as `--assume-filename`, requests
`--output-replacements-xml`, rejects DTD/entities and unexpected or incomplete
XML, verifies ordered non-overlapping in-file UTF-8 byte boundaries, converts
Clang tooling byte offsets/lengths to Unicode code-point positions, calculates
the formatted SHA-256 in memory, and retains no source/replacement text in
results or logs. UTF-8 BOM is supported and preserved; LF/CRLF/mixed endings,
non-BMP/combining text, EOF/no-final-newline, empty input, and multiple
replacements are explicitly covered. Ambiguous multiple insertions at one byte
offset are rejected.

Apply requires every client-provided snapshot SHA-256, obtains every structured
format result before a mutation, revalidates every snapshot including no-op
files, then invokes one Workspace `apply_text_edits` batch. A per-file
process/parse failure or a detected stale snapshot makes no workspace edit;
detected multi-file CAS conflicts are all-or-nothing. The underlying Workspace
staged commit attempts best-effort rollback on ordinary I/O failure. It is not a
filesystem-atomic transaction: rollback can fail, locks and a final external
writer can race, and crash/power loss can leave a partial result.

Default `style=file` discovery may traverse above the workspace and may follow a
symlinked project `.clang-format`/`_clang-format`; `InheritParentConfig` can do
the same. Those configurations are trusted operator/project input, not an MCP
input or sandboxed boundary, and their contents are neither returned nor logged.

clang-tidy is read-only in this phase. It accepts only explicit source files,
one validated generated workspace directory containing a regular non-link
`compile_commands.json`,
an optional bounded `--checks=<pattern>` value, and bounded timeout. It does not
publish `--fix`, `--fix-errors`, `--load`, `--extra-arg`,
`--extra-arg-before`, arbitrary `--config`, arbitrary header filters, or generic
arguments. Phase 1 uses a strict compiler-style output parser instead of
`--export-fixes` YAML, avoiding a new parser dependency and ensuring no
replacement can be applied. It supports Windows drive-colon locations and
multiline output; non-diagnostic continuation/source/caret lines are discarded
rather than copied into messages. Option-like/response-like source names are prefixed by
an explicit relative directory and ForgeMCP never emits the `--` compiler-arg
delimiter. ANSI/control data and source/caret excerpts are discarded, and
absolute paths embedded in semantic messages are redacted. Clang's
one-based UTF-8 byte columns are converted only at validated code-point
boundaries. External and invalid diagnostics are counted separately; capture or
parser loss sets diagnostic completeness false, while timeout/tool failure is a
separate execution state.

The CMake/workspace project, discovered `.clang-tidy` configuration, and its
compile commands are a trust boundary: clang-tidy may process project-controlled
frontend/plugin flags and external includes, and ForgeMCP is not a sandbox.
ForgeMCP adds no plugin-loading flag and exposes no arbitrary clang-tidy plugin
loading over MCP.
Raw diagnostics, compiler arguments, source, output, environment values, and
external-file contents never enter logs or public raw-output fields.

Sanitizer scope is parser-only. The parser accepts bounded text, recognizes ASan,
UBSan, and unknown fallback reports, strips controls, returns fixed normalized
summaries rather than raw report copies, bounded workspace-only frames and opaque
addresses, hides path-like external frames, and marks malformed/partial/multiple
or truncated reports. It launches no program or symbolizer, uses no network, and
fetches no source or symbols. A future `sanitizer__run` requires a separate
binary-execution/environment policy.

## Consequences

The quality feature gives stable bounded status, formatting, diagnostics, and
report parsing while preserving the Process Runtime and Workspace as the only
process/file authority. Intentional Phase 1 limits are no formatting of files
whose structured tool output is truncated, no auto-fix, no generic runner, no
untrusted compilation database sandbox, no sanitizer execution, and no promise
of crash-atomic filesystem transactions.
