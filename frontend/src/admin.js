import { getToolUiResourceUri } from "@modelcontextprotocol/ext-apps/app-bridge";
import { mountInvocationWidget } from "./admin-widget.js";
import { createSchemaForm } from "./admin-form.js";
import { createResourceView } from "./admin-resource.js";
import "./shared/widget.css";
import "./admin.css";

/** Build the local admin shell without interpreting server-controlled text as HTML. */
const root = document.getElementById("admin");
const state = {
  instances: [], instanceId: null, snapshot: null, page: "history", callId: null,
  call: null, resultMode: "widget", filter: "", controls: false, toolName: "",
  session: null, elicitations: [],
  catalogKind: "tools", catalogFilter: "", catalogSelection: null,
};
const commandDrafts = new Map();
const executingCommands = new Set();
let selectionGeneration = 0;
let refreshGeneration = 0;
let widgetGeneration = 0;
let widget = null;
let widgetAbort = null;
let detailKey = null;
let refreshTimer = null;
let events = null;
let refreshing = false;
let refreshAgain = false;

/** Create a text-only DOM element with optional children. */
function element(tag, className, text, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  node.append(...children);
  return node;
}

/** Create an explicit button action. */
function button(text, handler, className = "") {
  const node = element("button", className, text);
  node.type = "button";
  node.addEventListener("click", handler);
  return node;
}

/** Preserve the complete source value in a selectable JSON view. */
function json(value) {
  return element("pre", "admin-pre", JSON.stringify(value, null, 2) ?? "null");
}

/** Accept SDK list wrappers as well as the admin API's arrays. */
function collection(value, key) {
  if (Array.isArray(value)) return value;
  return Array.isArray(value?.[key]) ? value[key] : [];
}

/** Display source timestamps with the browser's explicit local timezone. */
function timestamp(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? String(value)
    : parsed.toLocaleString(undefined, { timeZoneName: "short" });
}

/** Derive a short instance label while retaining its complete path elsewhere. */
function instanceName(instance) {
  return String(instance.project_root ?? instance.id).replace(/[\\/]+$/, "").split(/[\\/]/).pop() || instance.id;
}

/** Use the selected theme consistently in the shell and connected Apps. */
function currentTheme() {
  return theme.value === "system"
    ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
    : theme.value;
}

/** Apply theme changes without remounting the selected invocation. */
function applyTheme() {
  document.documentElement.style.colorScheme = currentTheme();
  widget?.refreshContext();
}

const heading = element("div", "admin-brand", null, [
  element("span", "admin-brand-mark", "F"),
  element("div", "", null, [element("h1", "", "ForgeMCP"), element("span", "admin-brand-caption", "Local administration")]),
]);
const connection = element("span", "admin-connection", "Connecting");
connection.setAttribute("role", "status");
const controls = element("input");
controls.type = "checkbox";
controls.addEventListener("change", () => {
  state.controls = controls.checked;
  renderPageTabs();
  renderPage();
});
const controlsLabel = element("label", "admin-controls-toggle", null, [controls, document.createTextNode("Enable command controls")]);
const theme = element("select");
theme.setAttribute("aria-label", "Color theme");
for (const value of ["system", "dark", "light"]) {
  const option = element("option", "", value[0].toUpperCase() + value.slice(1));
  option.value = value;
  theme.append(option);
}
theme.addEventListener("change", applyTheme);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
const header = element("header", "admin-header", null, [heading, connection, controlsLabel, theme]);
const instanceTabs = element("nav", "admin-instances");
instanceTabs.setAttribute("aria-label", "Server instances");
instanceTabs.setAttribute("role", "tablist");
const instanceInfo = element("div", "admin-statusbar");
const pageTabs = element("nav", "admin-pages");
pageTabs.setAttribute("aria-label", "Instance views");
const notice = element("p", "admin-notice");
notice.setAttribute("role", "status");
notice.hidden = true;
const elicitationPanel = element("section", "admin-elicitations");
elicitationPanel.setAttribute("aria-label", "Pending confirmation requests");
const body = element("div", "admin-body");
root.append(header, instanceTabs, instanceInfo, pageTabs, notice, elicitationPanel, body);
applyTheme();

/** Display recoverable API failures without removing an already visible result. */
function inform(message) {
  notice.textContent = message ?? "";
  notice.hidden = !message;
}

/** Read only same-origin admin routes; mutations carry the session CSRF token. */
async function api(path, { method = "GET", value, signal, bootstrap } = {}) {
  const headers = { Accept: "application/json" };
  if (bootstrap) headers.Authorization = `Bearer ${bootstrap}`;
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    headers["X-ForgeMCP-Token"] = state.session?.token ?? "";
  }
  const response = await fetch(path, {
    method, headers, credentials: "same-origin", cache: "no-store", signal,
    ...(value === undefined ? {} : { body: JSON.stringify(value) }),
  });
  let data;
  try { data = await response.json(); } catch { data = null; }
  if (!response.ok) {
    if (response.status === 401) {
      throw new Error("Open this panel with forgemcp --open-admin to establish a local session.");
    }
    const detail = data?.error?.message ?? data?.error ?? data?.detail ?? response.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

/** Construct instance-scoped routes without treating IDs as URL path syntax. */
function instancePath(suffix = "", id = state.instanceId) {
  return `/api/instances/${encodeURIComponent(id)}${suffix}`;
}

/** Invalidate pending widget requests and tear down its original invocation. */
function clearWidget() {
  widgetGeneration++;
  widgetAbort?.abort();
  widgetAbort = null;
  const previous = widget;
  widget = null;
  if (previous) void previous.dispose();
}

/** Keep browser focus and selection on a named server instance. */
async function selectInstance(id) {
  if (id === state.instanceId && state.snapshot) return;
  selectionGeneration++;
  state.instanceId = id;
  state.snapshot = null;
  state.call = null;
  state.callId = null;
  state.elicitations = [];
  state.catalogSelection = null;
  state.catalogFilter = "";
  state.controls = false;
  controls.checked = false;
  detailKey = null;
  clearWidget();
  renderInstances();
  renderInstanceInfo();
  renderPageTabs();
  renderElicitations();
  body.replaceChildren(element("p", "admin-empty", "Loading instance…"));
  await refreshSelected();
}

/** Render all known independent instances as accessible keyboard-selectable tabs. */
function renderInstances() {
  instanceTabs.replaceChildren();
  for (const instance of state.instances) {
    const dot = element("span", "admin-dot");
    dot.dataset.connected = String(Boolean(instance.connected));
    dot.setAttribute("aria-hidden", "true");
    const tab = button(`${instanceName(instance)} · ${instance.pid ?? "—"}`, () => { void selectInstance(instance.id); }, "admin-instance-tab");
    tab.prepend(dot);
    tab.title = `${instance.project_root ?? ""}\n${instance.id}`;
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(instance.id === state.instanceId));
    tab.tabIndex = instance.id === state.instanceId ? 0 : -1;
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const tabs = [...instanceTabs.children];
      const index = tabs.indexOf(tab);
      const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
        : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
      tabs[next].focus();
      tabs[next].click();
    });
    instanceTabs.append(tab);
  }
}

/** Show connection and workspace identity without hiding long paths. */
function renderInstanceInfo() {
  const instance = state.instances.find((item) => item.id === state.instanceId) ?? state.snapshot?.instance;
  controls.disabled = !instance?.connected;
  instanceInfo.replaceChildren();
  if (!instance) return;
  for (const [label, value] of [
    ["Workspace", instance.project_root],
    ["Process", instance.pid ?? "—"],
    ["Version", instance.version ?? "—"],
    ["Started", timestamp(instance.started_at)],
  ]) {
    if (value !== undefined) instanceInfo.append(element("span", "admin-instance-meta", null, [
      element("span", "admin-meta-label", label), element("span", "", value),
    ]));
  }
}

/** Switch read-only views without introducing hidden tool executions. */
function renderPageTabs() {
  pageTabs.replaceChildren();
  if (!state.instanceId) return;
  for (const [key, label] of [["history", "Activity"], ["catalog", "Tools & resources"], ["monitors", "Monitors"], ["settings", "Settings"]]) {
    const tab = button(label, () => {
      if (key === state.page) return;
      state.page = key;
      detailKey = null;
      renderPageTabs();
      renderPage();
    });
    tab.setAttribute("aria-current", state.page === key ? "page" : "false");
    tab.setAttribute("aria-pressed", String(state.page === key));
    pageTabs.append(tab);
  }
  pageTabs.append(button("Refresh", () => { void refreshAll(); }, "admin-refresh"));
}

/** Refresh instance metadata and coalesce overlapping SSE invalidations. */
async function refreshAll() {
  if (refreshing) { refreshAgain = true; return; }
  refreshing = true;
  try {
    const result = await api("/api/instances");
    state.instances = collection(result, "instances");
    renderInstances();
    renderInstanceInfo();
    if (!state.instanceId || !state.instances.some((item) => item.id === state.instanceId)) {
      if (state.instances.length) await selectInstance(state.instances[0].id);
      else {
        selectionGeneration++;
        state.instanceId = null;
        state.snapshot = null;
        state.call = null;
        state.callId = null;
        state.elicitations = [];
        detailKey = null;
        clearWidget();
        renderInstanceInfo();
        renderPageTabs();
        renderElicitations();
        body.replaceChildren(element("p", "admin-empty", "No connected ForgeMCP instances. Start a server to add its tab."));
      }
    } else await refreshSelected();
  } catch (error) { inform(error.message); }
  finally {
    refreshing = false;
    if (refreshAgain) { refreshAgain = false; scheduleRefresh(); }
  }
}

/** Fetch monitor and history snapshots without placing commands in the execution queue. */
async function refreshSelected() {
  if (!state.instanceId) return;
  const id = state.instanceId;
  const generation = ++refreshGeneration;
  const selection = selectionGeneration;
  try {
    const [snapshot, inquiries] = await Promise.all([
      api(instancePath("/snapshot", id)),
      api(instancePath("/elicitations", id)),
    ]);
    if (id !== state.instanceId || generation !== refreshGeneration || selection !== selectionGeneration) return;
    const unchangedState = JSON.stringify(state.snapshot?.state) === JSON.stringify(snapshot.state);
    state.snapshot = snapshot;
    state.elicitations = collection(inquiries, "requests");
    renderInstanceInfo();
    renderElicitations();
    if (state.page === "history" && body.querySelector(".admin-history")) {
      renderHistoryList();
      const latest = history().find((call) => call.id === state.callId);
      if (latest) { state.call = latest; renderCall(); }
    } else if (state.page === "catalog" && body.querySelector(".admin-catalog")) {
      const submit = body.querySelector(".admin-command-form button[type='submit']");
      if (submit) {
        const executing = executingCommands.has(`${state.instanceId}/${state.toolName}`);
        submit.disabled = executing || !snapshot.instance?.connected;
        submit.textContent = executing ? "Executing…" : "Run tool";
      }
      const enable = body.querySelector(".admin-command-disabled button");
      if (enable) enable.disabled = !snapshot.instance?.connected;
    } else if (!["monitors", "settings"].includes(state.page) || !unchangedState) {
      const scroll = body.scrollTop;
      const expanded = [...body.querySelectorAll("details[open] > summary")].map((summary) => summary.textContent);
      renderPage();
      for (const summary of body.querySelectorAll("details > summary")) {
        if (expanded.includes(summary.textContent)) summary.parentElement.open = true;
      }
      body.scrollTop = scroll;
    }
    inform(null);
  } catch (error) {
    if (id === state.instanceId && selection === selectionGeneration) inform(error.message);
  }
}

/** Schedule one snapshot refresh for a burst of live change notifications. */
function scheduleRefresh() {
  if (refreshTimer !== null) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; void refreshAll(); }, 150);
}

/** Read the complete returned history in reverse chronological display order. */
function history() {
  return collection(state.snapshot?.history, "calls");
}

/** Show the chosen page and mount at most one result widget. */
function renderPage() {
  clearWidget();
  detailKey = null;
  body.replaceChildren();
  if (!state.snapshot) {
    body.append(element("p", "admin-empty", state.instanceId ? "Loading instance…" : "No server instance selected."));
    return;
  }
  if (state.page === "history") renderHistory();
  else if (state.page === "monitors") renderMonitors();
  else if (state.page === "catalog") renderCatalog();
  else renderSettings();
}

/** Build a command list with local search and an independent invocation detail pane. */
function renderHistory() {
  const layout = element("section", "admin-history");
  const sidebar = element("div", "admin-history-sidebar");
  const filter = element("input");
  filter.type = "search";
  filter.placeholder = "Filter tool, status or arguments";
  filter.setAttribute("aria-label", "Filter invocation history");
  filter.value = state.filter;
  filter.addEventListener("input", () => { state.filter = filter.value; renderHistoryList(); });
  const count = element("span", "admin-history-count");
  sidebar.append(element("div", "admin-history-filter", null, [
    element("h2", "", "Activity"), element("p", "admin-muted", "Tool calls from every connected client."), filter, count,
  ]), element("div", "admin-history-list"));
  const detail = element("section", "admin-detail");
  detail.setAttribute("aria-label", "Selected invocation");
  layout.append(sidebar, detail);
  body.append(layout);
  const calls = history();
  if (!state.callId || !calls.some((call) => call.id === state.callId)) {
    state.call = calls.at(-1) ?? null;
    state.callId = state.call?.id ?? null;
  }
  renderHistoryList();
  renderCall();
}

/** Update command summaries while leaving an existing widget and its scroll state intact. */
function renderHistoryList() {
  const list = body.querySelector(".admin-history-list");
  if (!list) return;
  const scroll = list.scrollTop;
  const calls = history();
  const needle = state.filter.toLocaleLowerCase();
  const matched = calls.filter((call) => !needle
    || `${call.tool_name} ${call.status} ${call.source} ${call.id} ${JSON.stringify(call.arguments)}`.toLocaleLowerCase().includes(needle));
  body.querySelector(".admin-history-count").textContent = `${matched.length} / ${calls.length} commands`;
  list.replaceChildren();
  if (!matched.length) list.append(element("p", "admin-empty", calls.length ? "No matching commands." : "No commands have been received."));
  for (const call of [...matched].reverse()) {
    const row = button("", () => { void selectCall(call.id); }, "admin-call");
    row.setAttribute("aria-selected", String(call.id === state.callId));
    const outcome = element("span", "admin-state", call.status);
    outcome.dataset.state = call.status;
    const time = element("span", "admin-call-time", timestamp(call.started_at ?? call.queued_at ?? call.created_at));
    time.title = call.started_at ?? call.queued_at ?? call.created_at ?? "";
    const args = Object.entries(call.arguments ?? {}).map(([key, value]) => `${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`).join(" · ");
    const preview = element("span", "admin-call-preview", args || "No arguments");
    preview.title = args;
    row.append(element("span", "admin-call-name", call.tool_name), preview, time, outcome,
      element("span", "admin-call-source", call.source === "admin" ? "Admin panel" : "MCP client"));
    list.append(row);
  }
  list.scrollTop = scroll;
}

/** Resolve a selected invocation through its instance-scoped endpoint. */
async function selectCall(id) {
  state.callId = id;
  state.call = history().find((item) => item.id === id) ?? null;
  detailKey = null;
  renderHistoryList();
  renderCall();
  const instance = state.instanceId;
  const selection = selectionGeneration;
  try {
    const call = await api(instancePath(`/calls/${encodeURIComponent(id)}`, instance));
    if (state.instanceId !== instance || state.callId !== id || selection !== selectionGeneration) return;
    state.call = call;
    renderCall();
  } catch (error) {
    if (state.instanceId === instance && state.callId === id) inform(error.message);
  }
}

/** Render metadata values with independent wrapping. */
function metadata(entries) {
  const list = element("dl", "admin-detail-meta");
  for (const [name, value] of entries) {
    list.append(element("dt", "", name), element("dd", "", value ?? "—"));
  }
  return list;
}

/** Render the exact supplied invocation, including failures without structured results. */
function renderCall() {
  const panel = body.querySelector(".admin-detail");
  if (!panel) return;
  const call = state.call;
  if (!call) {
    clearWidget();
    panel.replaceChildren(element("p", "admin-empty", "Select a command to inspect its input and result."));
    return;
  }
  const key = `${state.instanceId}/${call.id}/${call.status}/${call.finished_at ?? ""}/${Boolean(call.result)}/${state.resultMode}`;
  if (detailKey === key) {
    const progress = panel.querySelector(".admin-progress");
    if (progress) progress.textContent = progressText(call.progress);
    return;
  }
  detailKey = key;
  clearWidget();
  panel.replaceChildren();
  const outcome = element("span", "admin-state", call.status);
  outcome.dataset.state = call.status;
  const title = element("div", "admin-detail-heading", null, [element("h2", "", call.tool_name), outcome]);
  if (call.source === "admin" && call.status === "running") {
    const cancel = button("Cancel command", async () => {
      const instance = state.instanceId;
      cancel.disabled = true;
      try {
        await api(instancePath(`/calls/${encodeURIComponent(call.id)}/cancel`, instance), { method: "POST", value: {} });
        if (instance === state.instanceId) await refreshSelected();
      } catch (error) {
        if (instance === state.instanceId) inform(error.message);
        cancel.disabled = !state.controls;
      }
    });
    cancel.disabled = !state.controls;
    cancel.title = state.controls ? "Cancel this admin invocation" : "Enable command controls to cancel this invocation";
    title.append(cancel);
  }
  panel.append(title);
  panel.append(metadata([
    ["Call ID", call.id], ["Source", call.source],
    ["Started", timestamp(call.started_at)], ["Finished", timestamp(call.finished_at)],
  ]));
  panel.append(element("div", "admin-progress admin-muted", progressText(call.progress)));
  const argumentsView = element("details");
  argumentsView.append(element("summary", "", "Arguments"), json(call.arguments ?? {}));
  panel.append(argumentsView);
  if (call.error) panel.append(element("p", "admin-error", typeof call.error === "string" ? call.error : JSON.stringify(call.error, null, 2)));
  const tabs = element("div", "admin-result-tabs");
  for (const [mode, label] of [["widget", "Widget"], ["text", "Text"], ["json", "JSON"]]) {
    const tab = button(label, () => { state.resultMode = mode; renderCall(); });
    tab.setAttribute("aria-pressed", String(state.resultMode === mode));
    tabs.append(tab);
  }
  const result = element("div", "admin-result");
  panel.append(tabs, result);
  if (state.resultMode === "json") { result.append(json(call)); return; }
  if (state.resultMode === "text") { renderTextResult(result, call); return; }
  if (!call.result) {
    result.append(element("p", "admin-muted", ["queued", "running"].includes(call.status)
      ? "Waiting for the command result…" : "This invocation has no tool result. See JSON for its recorded state."));
    return;
  }
  const tool = collection(state.snapshot?.tools, "tools").find((item) => item.name === call.tool_name);
  let uri;
  try { uri = tool && getToolUiResourceUri(tool); } catch { /* Invalid metadata remains visible in JSON. */ }
  if (!uri) {
    result.append(element("p", "admin-muted", "No widget resource is available for this tool."));
    renderTextResult(result, call);
    return;
  }
  void showWidget(result, tool, call);
}

/** Describe only the latest progress snapshot rather than inventing completion percentages. */
function progressText(progress) {
  if (!progress) return "";
  if (Array.isArray(progress)) progress = progress.at(-1);
  if (!progress) return "";
  const value = progress.progress ?? progress.value;
  const total = progress.total;
  return [value === undefined ? "" : `${value}${total === null || total === undefined ? "" : ` / ${total}`}`, progress.message].filter(Boolean).join(" · ");
}

/** Keep the model-facing text fallback useful even when the App cannot load. */
function renderTextResult(container, call) {
  const content = call.result?.content ?? [];
  let texts = 0;
  for (const item of content) {
    if (item.type === "text" && typeof item.text === "string") {
      container.append(element("pre", "admin-pre", item.text));
      texts++;
    }
  }
  if (!texts) container.append(element("p", "admin-muted", "No text content. The complete original response is available in JSON."));
}

/** Read the selected App and restrict its subsequent requests to this call's links. */
async function showWidget(container, tool, call) {
  const generation = ++widgetGeneration;
  const instance = state.instanceId;
  const abort = new AbortController();
  widgetAbort = abort;
  const waiting = element("p", "admin-muted", "Connecting result widget…");
  container.append(waiting);
  try {
    const mounted = await mountInvocationWidget({
      container, tool, call, sandboxOrigin: state.session.sandbox_origin,
      signal: abort.signal, theme: currentTheme,
      readResource: (uri) => {
        const query = new URLSearchParams({ uri });
        if (uri.startsWith("forgemcp://workspace/results/")) query.set("call_id", call.id);
        return api(`${instancePath("/resources", instance)}?${query}`, { signal: abort.signal });
      },
    });
    if (generation !== widgetGeneration) { await mounted.dispose(); return; }
    waiting.remove();
    widget = mounted;
  } catch (error) {
    if (generation !== widgetGeneration || abort.signal.aborted) return;
    waiting.remove();
    container.append(element("p", "admin-error", `Widget unavailable: ${error.message}`));
    renderTextResult(container, call);
  }
}

/** Project already supplied monitor snapshots; viewing monitors never calls MCP tools. */
function renderMonitors() {
  const section = element("section", "admin-section", null, [
    element("div", "admin-page-heading", null, [element("h2", "", "Service monitors"),
      element("p", "admin-muted", "Live snapshots from the running server. Updated automatically.")]),
  ]);
  const supplied = state.snapshot.state ?? {};
  const stats = element("div", "admin-stats");
  for (const [label, value] of [
    ["Running processes", supplied.processes?.running ?? 0],
    ["Completed processes", supplied.processes?.completed ?? 0],
    ["Toolsets", supplied.toolsets?.items?.length ?? 0],
    ["Clangd sessions", supplied.clangd?.sessions?.length ?? 0],
  ]) stats.append(element("div", "admin-stat", null, [element("span", "admin-muted", label), element("strong", "", value)]));
  section.append(stats);
  if (!Object.keys(supplied).length) section.append(element("p", "admin-muted", "No service state has been published."));
  const cards = element("div", "admin-cards");
  for (const [name, value] of Object.entries(supplied)) {
    if (name === "settings") continue;
    const card = element("section", "admin-card", null, [element("h3", "", name)]);
    card.append(stateFields(value));
    cards.append(card);
  }
  section.append(cards);
  const raw = element("details", "admin-catalog-group");
  raw.append(element("summary", "", "Complete state JSON"), json(supplied));
  section.append(raw);
  body.append(section);
}

/** Present retained state as labeled values and expandable record tables. */
function stateFields(value) {
  const container = element("div", "admin-state-fields");
  if (!value || typeof value !== "object") { container.append(element("p", "admin-muted", value ?? "No data yet")); return container; }
  const scalars = Object.entries(value).filter(([, item]) => item === null || typeof item !== "object");
  if (scalars.length) container.append(metadata(scalars.map(([name, item]) => [name.replaceAll("_", " "), item === null ? "Not available" : typeof item === "boolean" ? (item ? "Yes" : "No") : item])));
  for (const [name, item] of Object.entries(value)) {
    if (item === null || typeof item !== "object") continue;
    const details = element("details", "admin-state-group");
    details.append(element("summary", "", `${name.replaceAll("_", " ")}${Array.isArray(item) ? ` (${item.length})` : ""}`));
    if (Array.isArray(item)) {
      if (!item.length) details.append(element("p", "admin-muted", "No entries."));
      else if (item.every((record) => record && typeof record === "object")) details.append(recordsTable(item));
      else details.append(element("pre", "admin-pre", item.join("\n")));
    } else details.append(stateFields(item));
    container.append(details);
  }
  return container;
}

/** Show every monitor field in a scrollable table, retaining nested values as JSON. */
function recordsTable(records) {
  const keys = [...new Set(records.flatMap((record) => Object.keys(record)))];
  const table = element("table", "admin-table");
  const headings = element("tr");
  for (const key of keys) headings.append(element("th", "", key));
  table.append(element("thead", "", null, [headings]));
  const rows = element("tbody");
  for (const record of records) {
    const row = element("tr");
    for (const key of keys) {
      const value = record[key];
      const cell = element("td", "", value === undefined ? "—" : value === null ? "null"
        : typeof value === "object" ? JSON.stringify(value, null, 2) : String(value));
      cell.style.whiteSpace = "pre-wrap";
      row.append(cell);
    }
    rows.append(row);
  }
  table.append(rows);
  return element("div", "admin-table-scroll", null, [table]);
}

/** Expose server settings as a read-only snapshot. */
function renderSettings() {
  const section = element("section", "admin-section", null, [element("div", "admin-page-heading", null, [
    element("h2", "", "Server settings"), element("p", "admin-muted", "Read-only configuration of this running instance. Restart the server to apply changes."),
  ])]);
  const cards = element("div", "admin-cards");
  for (const [name, value] of [["Instance", state.snapshot.instance], ["Startup configuration", state.snapshot.state?.settings ?? {}]]) {
    cards.append(element("section", "admin-card", null, [element("h3", "", name), stateFields(value)]));
  }
  const raw = element("details", "admin-definition");
  raw.append(element("summary", "", "Complete settings JSON"), json({ instance: state.snapshot.instance, settings: state.snapshot.state?.settings ?? {} }));
  section.append(cards, raw);
  body.append(section);
}

/** Keep catalog choices separate from their working area so forms retain focus on refresh. */
function renderCatalog() {
  const layout = element("section", "admin-catalog");
  const sidebar = element("aside", "admin-catalog-sidebar");
  const search = element("input");
  search.type = "search";
  search.placeholder = "Find a tool or resource…";
  search.setAttribute("aria-label", "Search MCP catalog");
  search.value = state.catalogFilter;
  const kinds = element("div", "admin-catalog-kinds");
  const list = element("div", "admin-catalog-list");
  const detail = element("section", "admin-catalog-detail");
  detail.setAttribute("aria-label", "Selected catalog entry");
  const groups = {
    tools: collection(state.snapshot.tools, "tools"),
    resources: collection(state.snapshot.resources, "resources"),
    templates: collection(state.snapshot.resource_templates, "resourceTemplates"),
  };
  for (const [kind, label] of [["tools", "Tools"], ["resources", "Resources"], ["templates", "Templates"]]) {
    const tab = button("", () => {
      state.catalogKind = kind;
      state.catalogSelection = null;
      renderPage();
    });
    tab.append(document.createTextNode(label), element("span", "admin-count", groups[kind].length));
    tab.setAttribute("aria-pressed", String(state.catalogKind === kind));
    kinds.append(tab);
  }
  sidebar.append(element("div", "admin-catalog-filter", null, [
    element("h2", "", "MCP catalog"), search, kinds,
  ]), list);
  layout.append(sidebar, detail);
  body.append(layout);
  const records = groups[state.catalogKind];
  const recordKey = (record) => record.uri ?? record.uriTemplate ?? record.name;
  if (!records.some((record) => recordKey(record) === state.catalogSelection)) {
    state.catalogSelection = records[0] ? recordKey(records[0]) : null;
  }

  /** Filter navigation without replacing a partially filled command. */
  function renderList() {
    list.replaceChildren();
    const needle = state.catalogFilter.toLocaleLowerCase();
    const matched = records.filter((record) => `${record.name} ${record.description ?? ""} ${recordKey(record)}`.toLocaleLowerCase().includes(needle));
    list.append(element("p", "admin-list-count", `${matched.length} ${state.catalogKind}`));
    let group;
    for (const record of matched) {
      const namespace = record.name.split("_")[0];
      const category = state.catalogKind === "tools"
        ? ({ process: "Processes", processes: "Processes", toolset: "Toolsets", toolsets: "Toolsets" }[namespace] ?? namespace)
        : null;
      if (category && category !== group) {
        list.append(element("div", "admin-catalog-category", category));
        group = category;
      }
      const key = recordKey(record);
      const row = button("", () => {
        state.catalogSelection = key;
        renderList();
        renderEntry(record);
      }, "admin-catalog-entry");
      row.setAttribute("aria-pressed", String(key === state.catalogSelection));
      row.append(element("span", "admin-entry-name", record.name ?? key));
      if (record.description) row.append(element("span", "admin-entry-description", record.description.split("\n")[0]));
      row.title = key;
      list.append(row);
    }
    if (!matched.length) list.append(element("p", "admin-empty", "No matches. Try a different search."));
  }

  /** Inspect one original SDK definition and expose its matching operation. */
  function renderEntry(record) {
    detail.replaceChildren();
    if (!record) {
      detail.append(element("p", "admin-empty", "No entries registered in this category."));
      return;
    }
    const type = state.catalogKind === "tools" ? "Tool" : state.catalogKind === "templates" ? "Resource template" : "Resource";
    const top = element("div", "admin-entry-heading", null, [
      element("span", "admin-eyebrow", type), element("h2", "", record.name ?? recordKey(record)),
    ]);
    if (record.description) top.append(element("p", "admin-description", record.description));
    detail.append(top);
    if (state.catalogKind === "tools") {
      state.toolName = record.name;
      const badges = element("div", "admin-tool-badges");
      if (record.annotations?.readOnlyHint) badges.append(element("span", "admin-badge", "Read only"));
      if (record.annotations?.destructiveHint) badges.append(element("span", "admin-badge admin-badge-warning", "Can change files or state"));
      detail.append(badges);
      if (state.controls) detail.append(commandForm(record));
      else {
        const enable = button("Enable command controls", () => {
          controls.checked = true;
          state.controls = true;
          renderEntry(record);
        }, "admin-primary");
        enable.disabled = !state.snapshot.instance?.connected;
        detail.append(element("div", "admin-command-disabled", null, [
          element("h3", "", "Ready to run a tool?"),
          element("p", "admin-muted", "Enable command controls to fill in parameters and execute on this workspace."), enable,
        ]));
      }
    } else {
      const instance = state.instanceId;
      const generation = selectionGeneration;
      detail.append(createResourceView(record, {
        readResource: (uri) => api(`${instancePath("/resources", instance)}?${new URLSearchParams({ uri })}`),
        complete: async (ref, argument, contextArguments) => {
          const result = await api(instancePath("/completion", instance), {
            method: "POST", value: { ref, argument, context: { arguments: contextArguments } },
          });
          if (generation !== selectionGeneration) throw new DOMException("Instance changed.", "AbortError");
          return result;
        },
      }));
    }
    const definition = element("details", "admin-definition");
    definition.append(element("summary", "", "Definition & JSON schema"), json(record));
    detail.append(definition);
  }
  search.addEventListener("input", () => { state.catalogFilter = search.value; renderList(); });
  renderList();
  renderEntry(records.find((record) => recordKey(record) === state.catalogSelection));
}

/** Compose typed arguments, keeping an independent draft for each instance and tool. */
function commandForm(tool) {
  const key = `${state.instanceId}/${tool.name}`;
  let draft = commandDrafts.get(key);
  if (!draft) {
    draft = { mode: "fields", raw: "{}", fields: createSchemaForm(tool.inputSchema, {}) };
    commandDrafts.set(key, draft);
  }
  const form = element("form", "admin-command-form");
  const modes = element("div", "admin-composer-toolbar");
  modes.append(element("h3", "", "Parameters"));
  const editor = element("div", "admin-command-editor");
  const raw = element("textarea", "admin-json-input");
  raw.setAttribute("aria-label", "Tool arguments as JSON");
  raw.spellcheck = false;
  raw.value = draft.raw;
  raw.addEventListener("input", () => { draft.raw = raw.value; });
  const localError = element("p", "admin-error");
  localError.setAttribute("role", "alert");
  localError.hidden = true;

  /** Report argument errors next to the command being edited. */
  function showError(message) {
    localError.textContent = message ?? "";
    localError.hidden = !message;
  }

  /** Synchronize modes only after validating the current input to prevent data loss. */
  function readArguments() {
    const args = draft.mode === "fields" ? draft.fields.getValue() : JSON.parse(raw.value);
    if (!args || typeof args !== "object" || Array.isArray(args)) throw new Error("Arguments must be a JSON object.");
    return args;
  }

  /** Move the same form DOM between views, preserving partially entered values. */
  function renderEditor() {
    modes.replaceChildren(element("h3", "", "Parameters"));
    for (const [mode, label] of [["fields", "Form"], ["json", "JSON"]]) {
      const toggle = button(label, () => {
        if (mode === draft.mode) return;
        try {
          const value = draft.mode === "fields" ? draft.fields.getValue(false) : readArguments();
          if (mode === "json") { draft.raw = JSON.stringify(value, null, 2); raw.value = draft.raw; }
          else draft.fields.setValue(value);
          draft.mode = mode;
          showError(null);
          renderEditor();
        } catch (error) { showError(error.message); }
      });
      toggle.setAttribute("aria-pressed", String(draft.mode === mode));
      modes.append(toggle);
    }
    editor.replaceChildren(draft.mode === "fields" ? draft.fields.element : raw);
  }
  renderEditor();
  const submit = element("button", "admin-primary", executingCommands.has(key) ? "Executing…" : "Run tool");
  submit.type = "submit";
  submit.disabled = executingCommands.has(key) || !state.snapshot.instance?.connected;
  const footer = element("div", "admin-command-footer", null, [
    submit, element("span", "admin-muted", "Runs on the selected workspace. Results appear in Activity."),
  ]);
  form.append(modes, editor, localError, footer);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.controls || executingCommands.has(key)) return;
    let args;
    try { args = readArguments(); } catch (error) { showError(error.message); return; }
    const instance = state.instanceId;
    const selection = selectionGeneration;
    executingCommands.add(key);
    submit.disabled = true;
    submit.textContent = "Executing…";
    showError(null);
    inform(null);
    try {
      await api(instancePath(`/tools/${encodeURIComponent(tool.name)}`, instance), { method: "POST", value: { arguments: args } });
      if (selection === selectionGeneration) {
        if (form.isConnected) {
          state.page = "history";
          state.callId = null;
          renderPageTabs();
        }
        await refreshSelected();
      }
    } catch (error) {
      if (selection === selectionGeneration) {
        if (form.isConnected) showError(error.message);
        else inform(error.message);
      }
    }
    finally {
      executingCommands.delete(key);
      if (selection === selectionGeneration) {
        submit.disabled = !state.snapshot.instance?.connected;
        submit.textContent = "Run tool";
      }
    }
  });
  return form;
}

/** Display pending SDK elicitation requests independently of execution controls. */
function renderElicitations() {
  if (elicitationPanel.dataset.ids === JSON.stringify(state.elicitations.map((item) => item.id))) return;
  elicitationPanel.dataset.ids = JSON.stringify(state.elicitations.map((item) => item.id));
  elicitationPanel.replaceChildren();
  if (!state.elicitations.length) return;
  const section = element("div", "admin-section", null, [element("h2", "", "Confirmation requested")]);
  for (const request of state.elicitations) {
    const params = request.params ?? request.request ?? request;
    const form = element("form", "admin-command-form");
    form.append(element("p", "", params.message ?? "The server is requesting confirmation."));
    const schema = params.requestedSchema ?? params.requested_schema ?? {};
    const urlMode = params.mode === "url";
    if (urlMode && typeof params.url === "string") {
      try {
        const url = new URL(params.url);
        if (["http:", "https:"].includes(url.protocol)) {
          const link = element("a", "admin-confirmation-link", url.href);
          link.href = url.href;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          form.append(link);
        } else form.append(element("p", "", params.url));
      } catch { form.append(element("p", "", params.url)); }
    }
    const details = element("details");
    details.append(element("summary", "", "Requested schema"), json(schema));
    form.append(details);
    const content = createSchemaForm(schema, {});
    if (!urlMode) form.append(content.element);
    const actions = element("div", "admin-result-tabs");
    for (const action of ["accept", "decline", "cancel"]) {
      actions.append(button(action[0].toUpperCase() + action.slice(1), async () => {
        let result = { action };
        if (action === "accept" && !urlMode) {
          try {
            const value = content.getValue();
            result = { action, content: value };
          } catch (error) { inform(error.message); return; }
        }
        const instance = state.instanceId;
        const buttons = [...actions.querySelectorAll("button")];
        for (const item of buttons) item.disabled = true;
        try {
          await api(instancePath(`/elicitations/${encodeURIComponent(request.id)}`, instance), { method: "POST", value: { result } });
          if (instance === state.instanceId) await refreshSelected();
        } catch (error) {
          if (instance === state.instanceId) inform(error.message);
          for (const item of buttons) item.disabled = false;
        }
      }));
    }
    form.append(actions);
    form.addEventListener("submit", (event) => event.preventDefault());
    section.append(form);
  }
  elicitationPanel.append(section);
}

/** Establish the cookie session before any catalog, event or instance requests. */
async function start() {
  try {
    const bootstrap = new URLSearchParams(location.hash.slice(1)).get("token");
    state.session = await api("/api/session", { bootstrap });
    if (bootstrap) window.history.replaceState(null, "", `${location.pathname}${location.search}`);
    await refreshAll();
    events = new EventSource("/api/events");
    events.addEventListener("change", scheduleRefresh);
    events.addEventListener("reset", scheduleRefresh);
    events.onopen = () => { connection.textContent = "Live"; connection.dataset.live = "true"; scheduleRefresh(); };
    events.onerror = () => { connection.textContent = "Reconnecting"; connection.dataset.live = "false"; };
    document.addEventListener("visibilitychange", () => { if (!document.hidden) scheduleRefresh(); });
  } catch (error) {
    connection.textContent = "Unavailable";
    inform(error.message);
    body.replaceChildren(element("p", "admin-empty", "Use forgemcp --open-admin to open an authenticated admin panel."));
  }
}

window.addEventListener("pagehide", () => {
  events?.close();
  clearTimeout(refreshTimer);
  clearWidget();
});
void start();
