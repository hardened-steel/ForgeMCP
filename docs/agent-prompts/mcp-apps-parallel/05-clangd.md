# Implement clangd result Apps

Read `00-common-contract.md` first. Create branch `codex/apps-clangd` from the
common base.

## Tool coverage

Bind all 27 `clangd__*` tools currently listed in
`tests/acceptance_manifest.py`. Do not rename, add or remove tools.

## Resources

Use four shared assets:

1. `ui://forgemcp/clangd/session`
   - `status`, `start`, `stop`
2. `ui://forgemcp/clangd/insight`
   - `diagnostics`, `hover`, `completion`, `signature_help`
3. `ui://forgemcp/clangd/navigation`
   - `definition`, `references`, `declaration`, `type_definition`,
     `implementation`, `document_symbols`, `workspace_symbols`,
     `switch_source_header`
4. `ui://forgemcp/clangd/change-hierarchy`
   - `prepare_rename`, `rename`, `code_actions`, `apply_code_action`,
     `format_document`, `format_range`, `prepare_call_hierarchy`,
     `incoming_calls`, `outgoing_calls`, `prepare_type_hierarchy`,
     `supertypes`, `subtypes`

Use explicit unique App names corresponding to those resources.

## UX

Session is a small lifecycle/status terminal panel: availability, state,
version, document/diagnostic counts, synchronization and compilation database
status. Start/stop results are read-only confirmations in the same geometry.

Insight uses fixed list/detail regions:

- diagnostics grouped or locally filtered by severity;
- hover rendered as safe preformatted text, never Markdown/HTML execution;
- completion as dense candidates with kind/detail in the fixed detail strip;
- signature help as one active signature/parameter plus a local selector when
  multiple signatures were returned.

Navigation displays workspace-relative locations and symbols. Document symbols
may use an indented tree; workspace symbols and references use flat dense rows.
Selecting an item updates a fixed preview line. No Open/Go/Rename buttons.

Change/hierarchy displays only results already returned:

- prepare rename range/snapshot metadata;
- rename/code-action/format applied/no-op/conflict and affected relative files;
- code actions as selectable descriptions/diagnostic counts, without an Apply
  control;
- call/type hierarchy handles and returned edges/items as a compact local tree.

Opaque handles may be displayed only when they are already public and useful;
prefer short labels, never decode or persist them. Do not synthesize source
text, external URI data or raw LSP payloads.

Inspect all result models in `src/forgemcp/clangd/models.py` and actual plugin
handlers. Respect UTF position encoding, stale/fresh state, bounded handles and
workspace URI omission.

## Ownership

Edit only the clangd plugin for App registrations. Add sources under
`frontend/clangd-apps/`, assets under `src/forgemcp/apps/assets/`, and tests in
`tests/test_mcp_apps_clangd.py`.

Because this subsystem is large, share internal renderer utilities inside
`frontend/clangd-apps/`; do not modify global common code.

Finish with the worker report required by the common contract.

