# ForgeMCP agent guide

## Project

ForgeMCP is a Python MCP server providing structured and safe C++ development
capabilities: workspace operations, CMake/build/test, clangd integration, and
native debugging.

## Read first

Before designing or changing a module, read:

1. `README.md`
2. `docs/architecture.md`
3. Relevant decisions in `docs/adr/`
4. The tests closest to the module being changed

## Architecture rules

- `src/forgemcp/core/` owns application composition, configuration, lifecycle,
  service registration, error conversion, and logging.
- Core must not contain CMake, workspace, clangd, debugger, or process business
  logic.
- New capabilities belong in separate modules and are registered through
  `ServiceRegistry`.
- Domain models must be transport-neutral. Do not expose MCP, LSP, CMake, or
  DAP implementation types outside their adapters.
- File access must remain scoped to the configured workspace.
- Never write file contents or secrets to logs.

## Development conventions

- Python 3.11+; use type annotations for public APIs.
- Prefer explicit dependencies over implicit global state.
- Add or update unit tests for behaviour changes.
- Keep MCP stdio output clean; operational logs go to stderr.
- Do not change public contracts without documenting the decision in `docs/adr/`.

## Validation

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Agents may run the following local, non-destructive workspace commands without
additional confirmation:

```powershell
npm run build --prefix frontend
npm test --prefix frontend
.\.venv\Scripts\python.exe -m pytest ...
.\.venv\Scripts\python.exe -m compileall ...
git diff --check
```

`npm ci --prefix frontend` may need network access when dependencies are first
installed or the lockfile changes; any Codex sandbox approval for that command
is a host-policy decision, not something this repository can disable.

## Documentation ownership
Stable onboarding and rules: AGENTS.md
Current system design: docs/architecture.md
Rationale for irreversible decisions: docs/adr/
Work status and ordering: docs/roadmap.md
