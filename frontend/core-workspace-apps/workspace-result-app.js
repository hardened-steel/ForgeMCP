import { connectMcpApp } from "../common/mcp-app.js";

const MAX_FILES = 1000;
const MAX_PATH = 4096;
const MAX_RENDER_TEXT = 262144;
const MAX_RENDER_LINES = 2000;
const MAX_RESULT_TEXT = 1100000;
const root = document.getElementById("app");
const state = { result: null, selected: null, tornDown: false };
const CHANGE_KINDS = new Set(["created", "modified", "deleted"]);

function isObject(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
function safeText(value, maximum = 256) {
  if (typeof value !== "string") return "";
  const text = value.slice(0, maximum).replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/gu, (item) => `\\u${item.codePointAt(0).toString(16).padStart(4, "0")}`);
  return text + (value.length > maximum ? "…" : "");
}
function safeCodeLine(value) { return value.replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/gu, (item) => `\\u${item.codePointAt(0).toString(16).padStart(4, "0")}`); }
function resultPayload(result) {
  if (!isObject(result) || result.isError === true) return null;
  if (isObject(result.structuredContent)) return result.structuredContent;
  const content = Array.isArray(result.content) && result.content.length === 1 && isObject(result.content[0]) ? result.content[0] : null;
  if (typeof content?.text !== "string" || content.text.length > MAX_RESULT_TEXT) return null;
  try { const parsed = JSON.parse(content.text); return isObject(parsed) ? parsed : null; } catch { return null; }
}
function validSnapshot(value) {
  if (!isObject(value) || typeof value.exists !== "boolean") return null;
  if (value.size_bytes !== null && (!Number.isInteger(value.size_bytes) || value.size_bytes < 0)) return null;
  if (value.sha256 !== null && (typeof value.sha256 !== "string" || value.sha256.length !== 64)) return null;
  if (value.modified_at !== null && typeof value.modified_at !== "string") return null;
  if (typeof value.captured_at !== "string") return null;
  return { exists: value.exists, size: value.size_bytes, sha256: value.sha256, modified: safeText(value.modified_at, 64), captured: safeText(value.captured_at, 64) };
}
function validFile(value) { if (!isObject(value) || typeof value.path !== "string" || value.path.length > MAX_PATH) return null; const snapshot = validSnapshot(value.snapshot); return snapshot ? { path: safeText(value.path, MAX_PATH), snapshot } : null; }
function validChange(value) {
  if (!isObject(value) || typeof value.path !== "string" || value.path.length > MAX_PATH || !CHANGE_KINDS.has(value.kind)) return null;
  const before = value.before === null ? null : validSnapshot(value.before); const after = value.after === null ? null : validSnapshot(value.after);
  if ((value.before !== null && !before) || (value.after !== null && !after)) return null;
  return { path: safeText(value.path, MAX_PATH), kind: value.kind, before, after };
}
function validWorkspaceResult(result) {
  const value = resultPayload(result);
  if (!value) return null;
  if (value.ok === false) return { kind: "error" };
  if (Array.isArray(value.files)) { if (value.files.length > MAX_FILES) return null; const files = value.files.map(validFile); return files.every(Boolean) ? { kind: "files", files } : null; }
  if (typeof value.path === "string" && typeof value.text === "string") { const snapshot = validSnapshot(value.snapshot); const lines = value.text.length <= MAX_RENDER_TEXT ? value.text.split("\n") : null; if (!snapshot || value.path.length > MAX_PATH || !lines || lines.length > MAX_RENDER_LINES) return null; return { kind: "text", path: safeText(value.path, MAX_PATH), snapshot, lines: lines.map(safeCodeLine) }; }
  if (typeof value.path === "string") { const snapshot = validSnapshot(value.snapshot); return snapshot && value.path.length <= MAX_PATH ? { kind: "snapshot", path: safeText(value.path, MAX_PATH), snapshot } : null; }
  if (typeof value.applied === "boolean" && Array.isArray(value.changes) && value.changes.length <= MAX_FILES) { const changes = value.changes.map(validChange); return changes.every(Boolean) ? { kind: "mutation", applied: value.applied, changes } : null; }
  return null;
}
function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
function hash(value) { return value?.sha256 ? `${value.sha256.slice(0, 12)}…` : "none"; }
function snapshotSummary(snapshot) { return `exists=${snapshot.exists} · size=${snapshot.size ?? "none"} · sha256=${hash(snapshot)}`; }
function detailFor(result, selected) {
  if (result.kind === "text") return `path=${result.path} · ${snapshotSummary(result.snapshot)} · line=${selected?.line ?? 1}`;
  if (result.kind === "snapshot") return `path=${result.path} · ${snapshotSummary(result.snapshot)}`;
  if (result.kind === "files") return selected ? `path=${selected.path} · ${snapshotSummary(selected.snapshot)}` : "No file selected.";
  if (result.kind === "mutation") return selected ? `path=${selected.path} · ${selected.kind} · ${snapshotSummary(selected.after || selected.before)}` : result.applied ? "Applied with no file changes." : "Conflict; no files changed.";
  return "No safe workspace result is available.";
}
function select(item) { state.selected = item; render(); }
function renderFiles(result, viewport) {
  const list = el("ul", "forge-workspace-list");
  for (const file of result.files) { const item = el("li"); const row = el("button", "forge-workspace-row"); row.type = "button"; row.setAttribute("aria-selected", String(file === state.selected)); row.setAttribute("aria-label", `${file.path}; ${snapshotSummary(file.snapshot)}`); row.append(el("span", "forge-row-symbol", file.snapshot.exists ? "·" : "×"), el("span", "forge-row-path", file.path), el("span", "forge-row-meta", `${file.snapshot.size ?? "—"} B`)); row.addEventListener("click", () => select(file)); item.append(row); list.append(item); }
  if (!result.files.length) list.append(el("li", "forge-empty", "No files returned.")); viewport.append(list);
}
function renderText(result, viewport) {
  const code = el("div", "forge-code");
  for (const [index, line] of result.lines.entries()) { const row = el("button", "forge-code-line"); row.type = "button"; row.setAttribute("aria-pressed", String(state.selected?.line === index + 1)); row.setAttribute("aria-label", `Line ${index + 1}`); row.append(el("span", "forge-line-number", String(index + 1)), el("span", "forge-line-text", line)); row.addEventListener("click", () => select({ line: index + 1 })); code.append(row); }
  viewport.append(code);
}
function renderSnapshot(result, viewport) {
  const table = el("div", "forge-snapshot");
  for (const [key, value] of [["path", result.path], ["exists", String(result.snapshot.exists)], ["size bytes", String(result.snapshot.size ?? "none")], ["sha256", result.snapshot.sha256 || "none"], ["modified", result.snapshot.modified || "none"], ["captured", result.snapshot.captured]]) { const row = el("div", "forge-snapshot-row"); row.append(el("span", "forge-key", key), el("span", "forge-value", value)); table.append(row); }
  viewport.append(table);
}
function renderMutation(result, viewport) {
  const list = el("ul", "forge-workspace-list");
  for (const change of result.changes) { const item = el("li"); const row = el("button", `forge-workspace-row ${change.kind}`); row.type = "button"; row.setAttribute("aria-selected", String(change === state.selected)); row.setAttribute("aria-label", `${change.kind}: ${change.path}`); row.append(el("span", "forge-row-symbol", change.kind === "created" ? "+" : change.kind === "deleted" ? "−" : "~"), el("span", "forge-row-path", change.path), el("span", "forge-row-meta", change.kind)); row.addEventListener("click", () => select(change)); item.append(row); list.append(item); }
  if (!result.changes.length) list.append(el("li", "forge-empty", result.applied ? "No-op mutation; no files changed." : "Conflict; no files changed.")); viewport.append(list);
}
function render() {
  root.replaceChildren(); const panel = el("section", "forge-workspace"); panel.setAttribute("aria-label", "Workspace result viewer"); const header = el("header", "forge-workspace-header"); header.append(el("span", "forge-prompt", "$"), el("span", "forge-command", "workspace::result")); const result = state.result;
  if (!result) { header.append(el("span", "forge-result-state error", "× UNAVAILABLE")); panel.append(header, el("div", "forge-workspace-summary forge-failure", "Workspace result unavailable or exceeds the safe view limit."), el("div", "forge-workspace-viewport"), el("div", "forge-workspace-detail"), el("footer", "forge-workspace-footer", "no actions")); root.append(panel); return; }
  const stateLabel = result.kind === "mutation" ? result.applied ? "● APPLIED" : "! CONFLICT" : result.kind === "error" ? "× ERROR" : "● RECEIVED"; const stateClass = result.kind === "mutation" && !result.applied ? "conflict" : result.kind === "error" ? "error" : "received"; header.append(el("span", `forge-result-state ${stateClass}`, stateLabel));
  const summary = el("div", "forge-workspace-summary"); const count = result.kind === "files" ? `${result.files.length} files` : result.kind === "text" ? `${result.lines.length} lines` : result.kind === "mutation" ? `${result.changes.length} affected` : result.kind === "snapshot" ? "snapshot metadata" : "published error"; summary.append(el("span", "", result.kind), el("span", "", count), el("span", "forge-summary-note", "local selection only"));
  const viewport = el("div", "forge-workspace-viewport"); if (result.kind === "files") renderFiles(result, viewport); else if (result.kind === "text") renderText(result, viewport); else if (result.kind === "snapshot") renderSnapshot(result, viewport); else if (result.kind === "mutation") renderMutation(result, viewport); else viewport.append(el("div", "forge-empty", "Workspace request failed."));
  const detail = el("div", "forge-workspace-detail", detailFor(result, state.selected)); detail.setAttribute("aria-live", "polite"); panel.append(header, summary, viewport, detail, el("footer", "forge-workspace-footer", "bounded result · no filesystem actions")); root.append(panel);
}

connectMcpApp({ name: "forgemcp-workspace-result", ontoolinput: () => {}, ontoolresult: (result) => { state.result = validWorkspaceResult(result); state.selected = state.result?.kind === "files" ? state.result.files[0] || null : state.result?.kind === "mutation" ? state.result.changes[0] || null : state.result?.kind === "text" ? { line: 1 } : null; render(); }, onhostcontextchanged: () => {}, onteardown: () => { state.tornDown = true; } }).catch(() => { if (!state.tornDown) render(); });
render();
