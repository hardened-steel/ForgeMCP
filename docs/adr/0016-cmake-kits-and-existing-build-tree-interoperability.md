# ADR 0016: CMake kits are cached, path-free selections with separate build trees

## Context

Windows C++ hosts commonly expose multiple Visual Studio toolsets, `clang-cl`,
standalone LLVM, and PATH compilers.  A CMake cache is not safely portable
between all compiler/generator choices, while existing workspace build trees
may have been created by VS Code or another CMake client.

## Decision

`ToolchainDiscoveryService` remains the sole application-scoped native-tool
scanner.  It derives immutable public `CMakeKit` records and keeps exact C/C++
compiler paths plus a filtered VS environment in private `ToolchainProfile`
records. Kit IDs are deterministic hashes of canonical safe identity metadata,
not filesystem paths. MCP never returns executable/install paths, raw version
or probe output, Developer environment values, or compiler commands.

Each kit also carries path-free `origin`, `driver_mode`, and `abi` markers.
Standalone LLVM `clang++`, Visual Studio LLVM `clang++`, `clang-cl`, and MSVC
are therefore distinct public profiles even where their compiler family is
`clang`. `--toolchain llvm` selects the clang family; within it deterministic
automatic ranking prefers ready standalone LLVM, then ready Visual Studio LLVM,
then degraded candidates. Exact provider choice is made with an opaque kit ID
from `cmake__list_kits` through runtime `cmake__select_kit`, `--cmake-kit`, or
`FORGEMCP_CMAKE_KIT`; neither filesystem enumeration order nor opaque ID order
participates in ranking.

The CMake service owns application-local selected-kit state. Selection is a
non-filesystem mutation guarded by a monotonic optional CAS generation and is
discarded at application shutdown. Configure selection precedence is operation
kit, runtime selected kit, CLI `--cmake-kit`, `FORGEMCP_CMAKE_KIT`, then a
deterministic ready kit. An explicit CMake preset and an explicit ForgeMCP kit
are different toolchain workflows and return a structured conflict rather than
being silently combined.

Generator precedence is operation generator, configured CLI/environment
generator, existing cache generator, preset generator, selected kit preference,
then safe automatic selection. Existing cache generators are never changed.
For command-line generators CMake receives the private selected C/C++ compiler
paths and filtered environment; Visual Studio generators use their own
generator/toolset/platform semantics and do not receive incompatible compiler
cache values.

An explicit kit with no binary directory selects the workspace-relative
`build/forgemcp/<kit-id>` suggestion. Existing explicit directories, configured
build directories, preset `binaryDir`, and the legacy `build` default remain
backward-compatible. ForgeMCP never deletes an incompatible cache. It reports
the cached/requested generator and compiler family plus the safe suggested
directory.

`cmake__list_build_trees` is a read-only bounded scan of conventional build
patterns. It validates cache/File API metadata through Workspace policy, rejects
links/reparse/external source trees, and reports only safe summary metadata.
Compatible externally-created trees are adopted through normal File API,
build/CTest, and validated compilation-database paths; ForgeMCP neither knows
nor needs to know whether VS Code created them.

ForgeMCP does not read CMake Tools global state, active selection, user-local
kit files, `.vscode/settings.json`, setup scripts, arbitrary environments, or
commands. `.vscode/cmake-kits.json` is documented as unsupported external
format. CMake Presets remain the standard alternative workflow.

## Consequences

Multiple MCP clients can safely coordinate selection without configuration side
effects. A client that wants a different kit gets an isolated build directory
rather than an automatic cache deletion or generator rewrite. Discovery and
all read-only kit/resources/completion surfaces use cached state only. This is
not a sandbox: trusted CMake configure/build still executes project logic and
the selected compiler through CMake.

D2.4 records an external configuration before the ForgeMCP session starts and
hash-proves `cmake__list_build_trees` is read-only before adoption. Compatible
Ninja trees with a CRLF cache are exercised through File API targets, build and
CTest. Mismatched generators/driver modes/ABI/compiler families and malformed,
stale, oversized, escaped, excluded, replaced or source-mismatched metadata are
rejected without selecting a new kit, configuring, invoking a compiler, or
disclosing cached executable paths. Adoption validates interoperability only;
an IDE-owned tree is never a sandbox or trust grant.
