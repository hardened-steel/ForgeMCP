# ADR 0012: Centralize configuration, CLI precedence, and trusted Windows toolchain discovery

## Context

Phase-A features previously read a small set of environment variables in Core,
while Process Runtime, Quality, and the LLDB qualifier independently searched
PATH and conventional installation locations. That made precedence ambiguous,
could produce different executable choices, and did not prepare MSVC when the
server was started from an ordinary shell.

## Decision

`ForgeConfig` is immutable and is composed once from CLI over `FORGEMCP_*`
environment over defaults. Operation arguments apply above this configuration
at their individual validated boundary. It retains source metadata for every effective
setting and a private read-only host-environment snapshot. Its public sanitized
representation contains only source categories and configured markers; it never
contains environment values, secrets, or absolute host paths. The no-subcommand
`forgemcp` server command remains compatible, while `doctor` and
`print-config` are local, sanitized commands implemented with stdlib argparse.

The precedence for a CMake build directory is operation parameter, CLI,
environment, selected preset `binaryDir`, then `build`. Every result passes
Workspace policy. The configuration generator is applied only without a
configure preset.

One application-scoped `ToolchainDiscoveryService` selects exact files for all
CMake, compiler, clangd, Quality, and debugger consumers. Candidate order is
explicit CLI path, explicit environment path, active Developer environment,
selected Visual Studio instance, safe PATH, then known standalone locations.
All accepted files are absolute regular non-link/reparse files outside the
workspace, canonicalized and later protected by ProcessPolicy metadata
replacement checks. Project status reads the service's cache only.

On Windows the service invokes a standard-location `vswhere.exe` with fixed
arguments, bounds/parses JSON (including depth, count, duplicate, and malformed
instance checks), deterministically selects eligible Community,
Professional, Enterprise, or Build Tools instances (including prerelease),
checks VC components, exact instance-ID selectors, and host/target-specific
compiler paths. It may capture `VsDevCmd.bat` using only a fixed system
`cmd.exe /d /s /c call ... && set` fixed command form whose script was discovered under
the selected instance and whose architecture values are enums. Paths containing
cmd metacharacters that cannot be proved safe are rejected. The resulting
environment is bounded, case-normalized, and filters PATH/LIB/INCLUDE entries
to existing non-reparse trusted VS/system/SDK directories; it never preserves
the inherited user PATH or secret-looking variables. Only exact selected
CMake/CTest commands receive it.

`cppvsdbg` and `OpenDebugAD7` remain availability-only discoveries. They are
not automatic backends and do not alter the LLVM/DWARF DAP baseline in ADR 0009.

Phase D1 additionally derives CMake kits from this same cached discovery
service. `--cmake-kit` / `FORGEMCP_CMAKE_KIT` are opaque path-free initial
selection inputs; exact compiler paths and filtered environments remain private
application capabilities. See ADR 0016 for selection and existing-tree rules.

## Consequences

Applications are isolated from each other's CLI/environment snapshots, CMake
and CTest can inherit a filtered MSVC Developer environment from a normal shell,
and
doctor gives actionable but path-safe rejection categories. Discovery itself is
performed at application startup/composition or local doctor, not by
`project__status`. CMake, build, test, and all tool probes still execute trusted
workspace/tool code under the existing Process Runtime limits; this is not a
sandbox. Preset inheritance/macro expansion remains owned by CMake, so only a
direct safe `binaryDir` contributes to pre-configure default resolution.
