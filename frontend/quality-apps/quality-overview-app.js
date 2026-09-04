import { connectMcpApp } from "../common/mcp-app.js";

const MAX_FILES = 64;
const MAX_CHECKS = 2048;
const MAX_TEXT = 512;
const root = document.getElementById("app");
const state = { view: null, selected: null, filter: "", tornDown: false };

function isObject(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
function safeText(value, maximum = MAX_TEXT) {
  if (typeof value !== "string") return "";
  const text = value.slice(0, maximum).replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/gu, (item) => `\\u${item.codePointAt(0).toString(16).padStart(4, "0")}`);
  return text + (value.length > maximum ? "…" : "");
}
function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
function resultPayload(result) {
  if (!isObject(result) || result.isError === true) return null;
  if (isObject(result.structuredContent)) return result.structuredContent;
  const content = Array.isArray(result.content) && result.content.length === 1 && isObject(result.content[0]) ? result.content[0] : null;
  if (typeof content?.text !== "string" || content.text.length > 100000) return null;
  try { const parsed = JSON.parse(content.text); return isObject(parsed) ? parsed : null; } catch { return null; }
}
function validTool(value) {
  if (!isObject(value) || typeof value.available !== "boolean") return null;
  if (value.version !== null && value.version !== undefined && typeof value.version !== "string") return null;
  if (value.error !== null && value.error !== undefined && typeof value.error !== "string") return null;
  return { available: value.available, version: safeText(value.version, 128), error: safeText(value.error, 192) };
}
function validStatus(value) {
  if (!isObject(value) || !Array.isArray(value.sanitizer_parsers) || value.sanitizer_parsers.length > 8) return null;
  const format = validTool(value.clang_format); const tidy = validTool(value.clang_tidy);
  if (!format || !tidy || value.sanitizer_parsers.some((item) => typeof item !== "string" || item.length > 128)) return null;
  return { kind: "status", format, tidy, parsers: value.sanitizer_parsers.map((item) => safeText(item, 128)) };
}
function validFile(value) {
  if (!isObject(value) || typeof value.path !== "string" || value.path.length > 4096) return null;
  if (value.would_change !== null && value.would_change !== undefined && typeof value.would_change !== "boolean") return null;
  if (value.error !== null && value.error !== undefined && typeof value.error !== "string") return null;
  return { path: safeText(value.path, 320), changed: value.would_change, error: safeText(value.error, 192) };
}
function validFormat(value) {
  if (!isObject(value) || !Array.isArray(value.files) || value.files.length > MAX_FILES) return null;
  const files = value.files.map(validFile); if (files.some((item) => item === null)) return null;
  const applied = typeof value.applied === "boolean" ? value.applied : null;
  if (applied === null && typeof value.clean !== "boolean") return null;
  if (value.conflict !== undefined && typeof value.conflict !== "boolean") return null;
  return { kind: "format", files, applied, conflict: value.conflict === true, clean: value.clean === true };
}
function validChecks(value) {
  if (!isObject(value) || !Array.isArray(value.checks) || value.checks.length > MAX_CHECKS || typeof value.truncated !== "boolean") return null;
  if (value.checks.some((item) => typeof item !== "string" || item.length > 256)) return null;
  return { kind: "checks", checks: value.checks.map((item) => safeText(item, 256)), truncated: value.truncated };
}
function validView(result) { const value = resultPayload(result); return validStatus(value) || validFormat(value) || validChecks(value); }
function rowData() {
  if (state.view?.kind === "format") return state.view.files.map((file) => ({ label: file.path, kind: file.error ? "error" : file.changed ? "changed" : "noop", meta: file.error || (file.changed ? "would change" : "no-op") }));
  if (state.view?.kind === "checks") return state.view.checks.filter((item) => item.toLowerCase().includes(state.filter.toLowerCase())).map((item) => ({ label: item, kind: "check", meta: "check" }));
  return [];
}
function viewName() { return state.view?.kind === "status" ? "quality::status" : state.view?.kind === "checks" ? "clang-tidy checks" : state.view?.kind === "format" ? "clang-format results" : "quality result"; }
function stateLabel() { if (!state.view) return ["error", "× invalid result"]; if (state.view.kind === "checks" && state.view.truncated) return ["incomplete", "! truncated"]; if (state.view.kind === "format" && state.view.conflict) return ["error", "× conflict"]; return ["ready", "● bounded view"]; }
function renderTools(panel) {
  if (state.view?.kind !== "status") return;
  const tools = el("div", "forge-tools");
  for (const [name, tool] of [["clang-format", state.view.format], ["clang-tidy", state.view.tidy]]) {
    const row = el("div", "forge-tool"); row.append(el("span", `forge-symbol ${tool.available ? "" : "offline"}`, tool.available ? "●" : "×"), el("span", "forge-tool-name", name), el("span", "forge-tool-value", tool.available ? tool.version || "available" : tool.error || "unavailable")); tools.append(row);
  }
  panel.append(tools);
}
function renderRows(panel) {
  const label = el("div", "forge-label"); label.append(el("span", "", state.view?.kind === "checks" ? "local filter:" : state.view?.kind === "format" ? "snapshot-safe file metadata:" : "sanitizer parsers:"));
  if (state.view?.kind === "checks") { const input = el("input", "forge-search"); input.type = "search"; input.value = state.filter; input.setAttribute("aria-label", "Filter clang-tidy checks locally"); input.addEventListener("input", () => { state.filter = safeText(input.value, 128); state.selected = null; render(); }); label.append(input); }
  if (state.view?.kind === "status") label.append(el("span", "forge-status", state.view.parsers.join(" · ")));
  panel.append(label);
  const list = el("div", "forge-list"); const rows = rowData();
  if (!rows.length) list.append(el("div", "forge-row", state.view?.kind === "status" ? "No file or check result attached." : "No matching safe rows."));
  rows.forEach((row, index) => { const button = el("button", `forge-row ${row.kind}`); button.type = "button"; button.setAttribute("aria-pressed", String(index === state.selected)); button.setAttribute("aria-label", `${row.label}, ${row.meta}`); button.append(el("span", "forge-row-kind", row.kind === "changed" ? "!" : row.kind === "noop" ? "●" : row.kind === "error" ? "×" : "·"), el("span", "forge-file-path", row.label), el("span", "forge-row-meta", row.meta)); button.addEventListener("click", () => { state.selected = index; render(); }); list.append(button); });
  panel.append(list);
  const selected = rows[state.selected ?? 0]; const detail = el("div", "forge-detail"); detail.append(el("span", "forge-detail-label", "detail:"), el("span", "forge-detail-value", selected ? `${selected.meta} · ${selected.label}` : "No safe item selected.")); panel.append(detail);
}
function render() {
  root.replaceChildren(); const panel = el("section", "forge-quality"); panel.setAttribute("aria-label", "Compact terminal-style Quality overview"); const [stateClass, stateText] = stateLabel(); const header = el("header", "forge-header"); header.append(el("span", "forge-prompt", "$"), el("span", "forge-command", viewName()), el("span", `forge-status ${stateClass}`, stateText)); panel.append(header); renderTools(panel); if (state.view?.kind !== "status") renderRows(panel); else { panel.append(el("div", "forge-label", "Tool availability and read-only parser scope."), el("div", "forge-list", "Invoke a bound Quality tool to display its safe result."), el("div", "forge-detail", "No UI-originated action is available.")); } const footer = el("footer", "forge-footer"); footer.append(el("span", "", state.view?.kind === "checks" ? "type to filter · select locally" : "select rows locally"), el("span", "", "read-only · no actions")); panel.append(footer); root.append(panel);
}
connectMcpApp({ name: "forgemcp-quality-overview", ontoolinput: () => {}, ontoolresult: (result) => { state.view = validView(result); state.selected = null; state.filter = ""; render(); }, onhostcontextchanged: () => {}, onteardown: () => { state.tornDown = true; } }).catch(() => { if (!state.tornDown) render(); });
render();
