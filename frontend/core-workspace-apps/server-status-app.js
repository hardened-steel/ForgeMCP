import { connectMcpApp } from "../common/mcp-app.js";

const MAX_SERVICES = 64;
const MAX_TEXT = 256;
const root = document.getElementById("app");
const state = { status: null, tornDown: false };
const LIFECYCLE = new Set(["created", "running", "stopped"]);
const SYMBOL = { created: "…", running: "●", stopped: "■" };

function isObject(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
function safeText(value, maximum = MAX_TEXT) {
  if (typeof value !== "string") return "";
  const text = value.slice(0, maximum).replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/gu, (item) => `\\u${item.codePointAt(0).toString(16).padStart(4, "0")}`);
  return text + (value.length > maximum ? "…" : "");
}
function resultPayload(result) {
  if (!isObject(result) || result.isError === true) return null;
  if (isObject(result.structuredContent)) return result.structuredContent;
  const content = Array.isArray(result.content) && result.content.length === 1 && isObject(result.content[0]) ? result.content[0] : null;
  if (typeof content?.text !== "string" || content.text.length > 16384) return null;
  try { const parsed = JSON.parse(content.text); return isObject(parsed) ? parsed : null; } catch { return null; }
}
function validServerStatus(result) {
  const value = resultPayload(result);
  if (!value || !LIFECYCLE.has(value.state) || typeof value.version !== "string" || typeof value.workspace_root !== "string" || !Array.isArray(value.services) || value.services.length > MAX_SERVICES || value.services.some((service) => typeof service !== "string")) return null;
  return { state: value.state, version: safeText(value.version, 64), workspace: safeText(value.workspace_root, 64), services: value.services.map((service) => safeText(service, 96)) };
}
function el(tag, className, text) { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; }
function render() {
  root.replaceChildren();
  const panel = el("section", "forge-server");
  panel.setAttribute("aria-label", "ForgeMCP server status");
  const header = el("header", "forge-server-header");
  header.append(el("span", "forge-prompt", "$"), el("span", "forge-command", "forgemcp::status"));
  const status = state.status;
  if (!status) {
    header.append(el("span", "forge-server-state stopped", "■ UNAVAILABLE"));
    panel.append(header, el("div", "forge-server-failure", "Server status result unavailable."), el("footer", "forge-server-footer", "stdio · no actions"));
    root.append(panel); return;
  }
  header.append(el("span", `forge-server-state ${status.state}`, `${SYMBOL[status.state]} ${status.state.toUpperCase()}`));
  const rows = el("div", "forge-server-rows");
  const lifecycle = el("div", "forge-server-row"); lifecycle.append(el("span", "forge-key", "lifecycle"), el("span", "forge-value", status.state), el("span", "forge-key", "version"), el("span", "forge-value", status.version));
  const transport = el("div", "forge-server-row"); transport.append(el("span", "forge-key", "transport"), el("span", "forge-value", "stdio"), el("span", "forge-key", "workspace"), el("span", "forge-value", status.workspace));
  const services = el("div", "forge-server-row forge-services"); services.append(el("span", "forge-key", "services"), el("span", "forge-value", `${status.services.length} registered`), el("span", "forge-service-list", status.services.join(" · ") || "none"));
  rows.append(lifecycle, transport, services);
  panel.append(header, rows, el("footer", "forge-server-footer", "cached lifecycle data · no actions")); root.append(panel);
}

connectMcpApp({ name: "forgemcp-server-status", ontoolinput: () => {}, ontoolresult: (result) => { state.status = validServerStatus(result); render(); }, onhostcontextchanged: () => {}, onteardown: () => { state.tornDown = true; } }).catch(() => { if (!state.tornDown) render(); });
render();
