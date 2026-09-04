import { connectMcpApp } from "../common/mcp-app.js";

const MAX_COMPONENTS = 64;
const MAX_CAPABILITIES = 128;
const MAX_WARNINGS = 32;
const MAX_TEXT = 512;
const root = document.getElementById("app");
const state = { selected: null, preview: null, status: null, tornDown: false };
const STATES = new Set(["available", "unavailable", "idle", "starting", "active", "paused", "degraded", "failed", "stopped"]);
const SYMBOLS = { available: "●", unavailable: "×", idle: "·", starting: "…", active: "▶", paused: "‖", degraded: "!", failed: "×", stopped: "■" };

function isObject(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
function safeText(value, maximum = MAX_TEXT) {
  if (typeof value !== "string") return "";
  const bounded = value.slice(0, maximum);
  const escaped = bounded.replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/gu, (item) => `\\u${item.codePointAt(0).toString(16).padStart(4, "0")}`);
  return escaped + (value.length > maximum ? "…" : "");
}
function count(value, maximum) { return Number.isInteger(value) && value >= 0 && value <= maximum ? value : 0; }
function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
function validComponent(value) {
  if (!isObject(value) || typeof value.id !== "string" || typeof value.display_name !== "string" || !STATES.has(value.state) || typeof value.summary !== "string" || !Array.isArray(value.warnings) || value.warnings.length > MAX_WARNINGS) return null;
  return { id: safeText(value.id, 64), name: safeText(value.display_name), state: value.state, summary: safeText(value.summary), warnings: value.warnings.filter((warning) => typeof warning === "string").slice(0, MAX_WARNINGS).map((warning) => safeText(warning, 128)), observedAt: safeText(value.observed_at, 64) };
}
function resultPayload(result) {
  if (!isObject(result) || result.isError === true) return null;
  if (isObject(result.structuredContent)) return result.structuredContent;
  const content = Array.isArray(result.content) && result.content.length === 1 && isObject(result.content[0]) ? result.content[0] : null;
  if (typeof content?.text !== "string" || content.text.length > 100000) return null;
  try { const parsed = JSON.parse(content.text); return isObject(parsed) ? parsed : null; } catch { return null; }
}
function validStatus(result) {
  const value = resultPayload(result);
  if (!value || !["healthy", "degraded", "failed"].includes(value.health) || !["idle", "busy", "paused"].includes(value.activity) || !Array.isArray(value.components) || value.components.length > MAX_COMPONENTS || !Array.isArray(value.capabilities) || value.capabilities.length > MAX_CAPABILITIES || !Array.isArray(value.warnings) || value.warnings.length > MAX_WARNINGS) return null;
  return { health: value.health, activity: value.activity, components: value.components.map(validComponent).filter(Boolean), capabilities: value.capabilities.filter((item) => typeof item === "string").slice(0, MAX_CAPABILITIES), warnings: value.warnings.filter((item) => typeof item === "string").slice(0, MAX_WARNINGS), generatedAt: safeText(value.generated_at, 64) };
}
function currentComponent() { return state.preview || state.selected || state.status?.components[0] || null; }
function shortUtc(value) { const match = /^\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|\+00:00)$/u.exec(value); return match ? `${match[1]}Z` : ""; }
function detailMessage(component) { return component?.warnings[0] || component?.summary || "No safe component detail available."; }
function renderDetail() { const target = root.querySelector(".forge-project-detail"); const component = currentComponent(); const observed = shortUtc(component?.observedAt || ""); if (!target) return; target.replaceChildren(el("span", "forge-project-detail-label", "component:"), el("span", "forge-project-detail-value", component?.name || "none"), el("span", "forge-project-detail-kind", component ? `state: ${component.state}${observed ? ` · observed ${observed}` : ""}` : "state: unavailable"), el("span", "forge-project-detail-message", detailMessage(component))); }
function select(component) { state.selected = component || null; state.preview = null; renderComponents(); renderDetail(); }
function preview(component) { state.preview = component; renderDetail(); }
function renderComponents() {
  const target = root.querySelector(".forge-project-grid"); if (!target || !state.status) return; target.replaceChildren();
  if (!state.status.components.length) { target.append(el("div", "forge-project-empty", "No valid component snapshots.")); return; }
  for (const component of state.status.components) {
    const button = el("button", "forge-project-component"); button.type = "button"; button.setAttribute("aria-selected", String(component === state.selected)); button.setAttribute("aria-label", `${component.name}, ${component.state}. ${detailMessage(component)}`); button.append(el("span", `forge-status-symbol ${component.state}`, SYMBOLS[component.state]), el("span", "forge-component-name", component.name), el("span", "forge-component-state", component.state));
    button.addEventListener("click", () => select(component)); button.addEventListener("mouseenter", () => preview(component)); button.addEventListener("mouseleave", () => { state.preview = null; renderDetail(); }); button.addEventListener("focus", () => preview(component)); button.addEventListener("blur", () => { state.preview = null; renderDetail(); }); button.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); select(component); } if (event.key === "ArrowLeft" || event.key === "ArrowUp" || event.key === "ArrowRight" || event.key === "ArrowDown") { event.preventDefault(); const buttons = [...target.querySelectorAll("button")]; const index = buttons.indexOf(button); const columns = window.matchMedia("(max-width: 520px)").matches ? 2 : 3; const next = event.key === "ArrowLeft" ? index - 1 : event.key === "ArrowRight" ? index + 1 : event.key === "ArrowUp" ? index - columns : index + columns; (buttons[next] || button).focus(); } }); target.append(button);
  }
}
function render() {
  root.replaceChildren(); const panel = el("section", "forge-project"); panel.setAttribute("aria-label", "Compact terminal-style project status"); const status = state.status;
  const header = el("header", "forge-project-header"); header.append(el("span", "forge-prompt", "$"), el("span", "forge-project-command", "project::status")); if (status) { header.append(el("span", `forge-project-health ${status.health}`, status.health.toUpperCase()), el("span", "forge-project-activity", `/ ${status.activity.toUpperCase()}`)); } panel.append(header);
  if (!status) { panel.append(el("div", "forge-project-summary forge-project-failure", "Project status result unavailable."), el("div", "forge-project-grid"), el("div", "forge-project-detail"), el("footer", "forge-project-footer", "● error")); root.append(panel); renderDetail(); return; }
  const summary = el("div", "forge-project-summary"); summary.append(el("span", "", `${status.components.length} components`), el("span", "", `${status.capabilities.length} capabilities`), el("span", "", `${status.warnings.length} notices`)); const stamp = shortUtc(status.generatedAt); if (stamp) { const time = el("time", "", stamp); time.dateTime = status.generatedAt; summary.append(time); } panel.append(summary, el("div", "forge-project-grid")); const detail = el("div", "forge-project-detail"); detail.setAttribute("aria-live", "polite"); panel.append(detail); const footer = el("footer", "forge-project-footer"); footer.append(el("span", "", "click inspect · arrows navigate"), el("span", "", "cached snapshot · no actions")); panel.append(footer); root.append(panel); renderComponents(); renderDetail();
}

connectMcpApp({ ontoolinput: () => {}, ontoolresult: (result) => { state.status = validStatus(result); state.selected = state.status?.components[0] || null; state.preview = null; render(); }, onteardown: () => { state.tornDown = true; } }).catch(() => { if (!state.tornDown) render(); });
render();
