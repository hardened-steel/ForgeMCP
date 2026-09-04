import { connectMcpApp } from "../common/mcp-app.js";

const MAX_FILES = 512;
const MAX_PATH_LENGTH = 4096;
const MAX_BRANCH_LENGTH = 1024;
const root = document.getElementById("app");
const state = { filter: null, selected: null, status: null, tornDown: false };

function isObject(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
function safeText(value, maximum = MAX_PATH_LENGTH) {
  if (typeof value !== "string") return "";
  const text = value.slice(0, maximum).replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/gu, (item) => `\\u${item.codePointAt(0).toString(16).padStart(4, "0")}`);
  return text + (value.length > maximum ? "…" : "");
}
function count(value) { return Number.isInteger(value) && value >= 0 ? value : 0; }
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function validStatus(result) {
  const status = isObject(result) && result.isError !== true && isObject(result.structuredContent) ? result.structuredContent : null;
  if (!status || !["available", "unavailable", "error"].includes(status.repository) || !Array.isArray(status.files) || status.files.length > MAX_FILES) return null;
  return status;
}
function kind(file) {
  if (file.conflicted === true) return "conflicted";
  if (file.untracked === true) return "untracked";
  if (typeof file.staged_status === "string" && file.staged_status !== ".") return "staged";
  return "modified";
}
function matches(file, filter) { return !filter || kind(file) === filter; }
function statusCode(file) {
  if (file.untracked === true) return "??";
  return `${safeText(file.staged_status, 1) || "."}${safeText(file.unstaged_status, 1) || "."}`.replaceAll(".", "·");
}
function files() { return Array.isArray(state.status?.files) ? state.status.files.filter(isObject) : []; }
function select(file) { state.selected = file || null; renderDetail(); renderRows(); }
function filterCount(name) {
  const fields = { staged: "staged_count", modified: "unstaged_count", untracked: "untracked_count", conflicted: "conflicted_count" };
  return count(state.status?.[fields[name]]);
}
function detailText(file) {
  if (!file) return ["selected:", "none", ""];
  const original = safeText(file.original_path);
  const states = `index=${safeText(file.staged_status, 1) || "."} · worktree=${safeText(file.unstaged_status, 1) || "."}`;
  return ["selected:", safeText(file.path), `${states}${original ? ` · original=${original}` : ""}`];
}
function renderDetail() {
  const target = root.querySelector(".forge-detail");
  if (!target) return;
  const [label, path, states] = detailText(state.selected);
  target.replaceChildren(el("span", "forge-detail-label", label), el("span", "forge-detail-path", path), el("span", "forge-detail-states", states));
}
function renderRows() {
  const target = root.querySelector(".forge-files");
  if (!target) return;
  const visible = files().filter((file) => matches(file, state.filter));
  target.replaceChildren();
  if (!visible.length) target.append(el("li", "forge-empty", "No matching paths."));
  for (const file of visible) {
    const item = el("li");
    const row = el("button", `forge-file ${kind(file)}`);
    row.type = "button";
    row.setAttribute("aria-selected", String(file === state.selected));
    row.append(el("span", "forge-code", statusCode(file)), el("span", "forge-path", safeText(file.path)));
    row.addEventListener("click", () => select(file));
    item.append(row);
    target.append(item);
  }
  const shown = root.querySelector(".forge-filter-state");
  if (shown) shown.textContent = `${state.filter || "all"} · ${visible.length} shown`;
}
function filterButton(name, code, label, tip) {
  const button = el("button", `forge-filter ${name}`, `[${code} ${filterCount(name)} ${label}]`);
  button.type = "button";
  button.title = tip;
  button.setAttribute("aria-pressed", String(state.filter === name));
  button.addEventListener("click", () => {
    state.filter = state.filter === name ? null : name;
    const visible = files().filter((file) => matches(file, state.filter));
    if (!visible.includes(state.selected)) state.selected = visible[0] || null;
    render();
  });
  return button;
}
function branchLabel(status) {
  if (status.unborn === true) return "unborn";
  if (status.detached === true) return "detached";
  return safeText(status.branch, MAX_BRANCH_LENGTH) || "unknown";
}
function render() {
  root.replaceChildren();
  const panel = el("section", "forge-term");
  panel.setAttribute("aria-label", "Compact terminal-style Git status");
  const command = el("header", "forge-command");
  command.append(el("span", "forge-prompt", "$"), el("span", "forge-command-text", "git status --short --branch"));
  panel.append(command);
  const status = state.status;
  if (!status) {
    panel.append(el("div", "forge-branch forge-error", "Git status result unavailable."), el("div", "forge-filters"), el("ul", "forge-files"), el("div", "forge-detail"), el("footer", "forge-footer", "● error"));
    root.append(panel); return;
  }
  const sync = `↑${count(status.ahead)} ↓${count(status.behind)}`;
  const branch = el("div", "forge-branch");
  branch.append(el("span", "forge-branch-prefix", "##"), el("span", "forge-branch-name", branchLabel(status)), el("span", "forge-sync", sync));
  const strip = el("span", "forge-strip");
  strip.title = "One cell per changed path; color matches status.";
  strip.setAttribute("tabindex", "0");
  for (const file of files().slice(0, 16)) strip.append(el("i", kind(file)));
  branch.append(strip); panel.append(branch);
  const filters = el("div", "forge-filters");
  filters.setAttribute("aria-label", "Local status filters");
  filters.append(filterButton("staged", "S", "staged", "Show staged paths"), filterButton("modified", "M", "modified", "Show modified worktree paths"), filterButton("untracked", "?", "untracked", "Show untracked paths"), filterButton("conflicted", "!", "conflict", "Show merge conflicts"), el("span", "forge-filter-state"));
  panel.append(filters, el("ul", "forge-files"));
  const detail = el("div", "forge-detail"); detail.setAttribute("aria-live", "polite"); panel.append(detail);
  const footer = el("footer", "forge-footer");
  const total = count(status.staged_count) + count(status.unstaged_count) + count(status.untracked_count) + count(status.conflicted_count);
  const mode = status.repository !== "available" ? "error" : status.truncated === true ? "truncated" : status.incomplete === true ? "incomplete" : "complete";
  footer.append(el("span", "forge-total", total ? `${total} changed · ${sync}` : "working tree clean"), el("span", `forge-${mode}`, `● ${mode}`));
  panel.append(footer); root.append(panel); renderRows(); renderDetail();
}

connectMcpApp({
  name: "forgemcp-git-status",
  ontoolinput: () => {},
  ontoolresult: (result) => { state.status = validStatus(result); state.filter = null; state.selected = files()[0] || null; render(); },
  onhostcontextchanged: () => {},
  onteardown: () => { state.tornDown = true; },
}).catch(() => { if (!state.tornDown) render(); });
render();
