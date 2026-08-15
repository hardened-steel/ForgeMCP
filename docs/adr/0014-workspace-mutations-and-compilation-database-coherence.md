# ADR 0014: Workspace mutations drive bounded CMake and clangd coherence

## Context

Workspace CAS edits are the sole trusted file-writing boundary, but the MCP
surface previously offered no Workspace tools and feature caches could retain
stale CMake/clangd state after a successful ForgeMCP mutation. On Windows, the
default Visual Studio generator does not produce a compilation database, which
leaves clangd intelligence unnecessarily degraded for projects that can use
Ninja with the discovered MSVC Developer environment.

## Decision

Expose `workspace__list_files`, `workspace__read_text`,
`workspace__get_snapshot`, `workspace__apply_unified_patch`, and
`workspace__apply_text_edits` through a builtin Workspace plugin. Reads and
snapshots use workspace-relative paths. Mutations require a SHA-256 CAS value;
unified patches may create an absent file with an explicit null expectation,
but this public surface does not expose delete or rename operations.

`WorkspaceService` emits one ordered, content-free post-commit batch to an
application-local bounded mutation bus. Events contain a monotonic generation,
relative path, change kind, old/new snapshot metadata, and operation ID only.
Subscriber failures or saturation mark integration state degraded and cannot
roll back a committed filesystem change. No external filesystem watcher is
introduced; guarantees cover changes made through ForgeMCP.

CMake marks its cached profile stale after committed changes to CMake lists,
`.cmake` files, presets, or a configured workspace toolchain file. It never
runs configure automatically. The compilation-database mode is source-aware
configuration: `auto` (default), `required`, or `off`. In `auto`, an unpinned
empty build tree uses qualified Ninja when available and adds
`CMAKE_EXPORT_COMPILE_COMMANDS=ON`; on Windows it inherits the selected MSVC
Developer environment through the existing CMake Process Runtime boundary.
An explicitly selected Visual Studio generator is never replaced by Ninja.

CMake reads the actual generator from the generated cache, rejects generator
changes in an existing tree, and validates `compile_commands.json` only inside
the selected generated build directory. It returns metadata/fingerprint only,
never commands, source content, or external paths. `required` fails with a
structured error if known generator support is absent or the post-configure
database is missing/invalid; filesystem changes from configure are not called
atomic and are not rolled back.

clangd subscribes to Workspace batches. It sends bounded full-document
`didChange` only for previously tracked documents, invalidates stale
diagnostics/opaque handles, and records untracked paths as dirty for lazy use.
It also consumes validated compilation-database revisions: an active session
does one bounded controlled restart/reinitialize on fingerprint change and
reopens only previously tracked documents. Restart failure degrades clangd
without changing the successful CMake configure result.

## Consequences

The public `clangd__start` database directory is optional: the latest validated
CMake profile is used automatically, while `off` deliberately permits clangd's
fallback command inference. Raw `compile_commands.json` remains trusted
project input for native tooling, not a sandbox boundary. Resources, prompts,
completion, external watching, Git, arbitrary filesystem access, binary edits,
and unrestricted writes remain out of scope.
