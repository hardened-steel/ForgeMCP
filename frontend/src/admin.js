import { getToolUiResourceUri } from "@modelcontextprotocol/ext-apps/app-bridge";
import { mountInvocationWidget } from "./admin-widget.js";
import "./shared/widget.css";
import "./admin.css";

/** Build the local admin shell without interpreting server-controlled text as HTML. */
const root = document.getElementById("admin");
const state = {
  instances: [], instanceId: null, snapshot: null, page: "history", callId: null,
  call: null, resultMode: "widget", filter: "", controls: false, toolName: "",
  arguments: "{}", executing: false, session: null, elicitations: [],
};
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

const heading = element("h1", "", "ForgeMCP / admin");
const connection = element("span", "admin-connection", "Connecting");
connection.setAttribute("role", "status");
const controls = element("input");
controls.type = "checkbox";
controls.addEventListener("change", () => {
  state.controls = controls.checked;
  renderPageTabs();
  renderPage();
});
const controlsLabel = element("label", "", null, [controls, document.createTextNode("Enable command controls")]);
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
  for (const value of [
    instance.connected ? "Connected" : "Disconnected",
    instance.project_root,
    `PID ${instance.pid ?? "—"}`,
    `v${instance.version ?? "—"}`,
    `Started ${timestamp(instance.started_at)}`,
  ]) {
    if (value !== undefined) instanceInfo.append(element("span", "", value));
  }
}

/** Switch read-only views without introducing hidden tool executions. */
function renderPageTabs() {
  pageTabs.replaceChildren();
  if (!state.instanceId) return;
  for (const [key, label] of [["history", "History"], ["monitors", "Monitors"], ["catalog", "Catalog"], ["settings", "Settings"]]) {
    const tab = button(label, () => {
      if (key === state.page) return;
      state.page = key;
      detailKey = null;
      renderPageTabs();
      renderPage();
    });
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
    state.snapshot = snapshot;
    state.elicitations = collection(inquiries, "requests");
    renderInstanceInfo();
    renderElicitations();
    if (state.page === "history" && body.querySelector(".admin-history")) {
      renderHistoryList();
      const latest = history().find((call) => call.id === state.callId);
      if (latest) { state.call = latest; renderCall(); }
    } else if (state.page === "catalog" && body.querySelector(".admin-cards")) {
      const submit = body.querySelector(".admin-command-form button[type='submit']");
      if (submit) submit.disabled = state.executing || !snapshot.instance?.connected;
    } else {
      const scroll = body.scrollTop;
      renderPage();
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
  sidebar.append(element("div", "admin-history-filter", null, [filter, count]), element("div", "admin-history-list"));
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
    row.append(element("span", "admin-call-name", call.tool_name), time, outcome);
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
  const section = element("section", "admin-section", null, [element("h2", "", "Service monitors")]);
  const supplied = state.snapshot.state ?? {};
  if (!Object.keys(supplied).length) section.append(element("p", "admin-muted", "No service state has been published."));
  const cards = element("div", "admin-cards");
  for (const [name, value] of Object.entries(supplied)) {
    if (name === "settings") continue;
    const card = element("section", "admin-card", null, [element("h3", "", name)]);
    const items = Array.isArray(value) ? value : value?.processes ?? value?.sessions ?? value?.contexts ?? value?.toolsets;
    if (Array.isArray(items) && items.length && items.every((item) => item && typeof item === "object")) {
      card.append(recordsTable(items));
    } else card.append(json(value));
    cards.append(card);
  }
  section.append(cards);
  const raw = element("details", "admin-catalog-group");
  raw.append(element("summary", "", "Complete state JSON"), json(supplied));
  section.append(raw);
  body.append(section);
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
  const section = element("section", "admin-section", null, [element("h2", "", "Server settings")]);
  section.append(element("p", "admin-muted", "Settings reflect the running server configuration."));
  section.append(json({ instance: state.snapshot.instance, settings: state.snapshot.state?.settings ?? {} }));
  body.append(section);
}

/** List the original SDK catalog and optionally expose explicit command controls. */
function renderCatalog() {
  const section = element("section", "admin-section", null, [element("h2", "", "MCP catalog")]);
  const tools = collection(state.snapshot.tools, "tools");
  if (state.controls) section.append(commandForm(tools));
  else section.append(element("p", "admin-muted", "Command controls are disabled. Enable them above to prepare and execute a tool."));
  const cards = element("div", "admin-cards");
  for (const tool of tools) {
    const card = element("article", "admin-card", null, [element("h3", "", tool.name)]);
    if (tool.description) card.append(element("p", "", tool.description));
    const definition = element("details");
    definition.append(element("summary", "", "Tool definition and schemas"), json(tool));
    card.append(definition);
    if (state.controls) card.append(button("Prepare command", () => {
      state.toolName = tool.name;
      state.arguments = "{}";
      renderPage();
      body.querySelector("textarea")?.focus();
    }));
    cards.append(card);
  }
  section.append(cards);
  for (const [label, value, key] of [
    ["Resources", state.snapshot.resources, "resources"],
    ["Resource templates", state.snapshot.resource_templates, "resourceTemplates"],
  ]) {
    const group = element("section", "admin-catalog-group", null, [element("h3", "", label)]);
    const records = collection(value, key);
    if (!records.length) group.append(element("p", "admin-muted", "None registered."));
    for (const resource of records) {
      const details = element("details", "admin-card");
      details.append(element("summary", "admin-resource-title", resource.uri ?? resource.uriTemplate ?? resource.name), json(resource));
      if (typeof resource.uri === "string") {
        const preview = element("div");
        const instance = state.instanceId;
        const read = button("Read resource", async () => {
          read.disabled = true;
          try {
            const query = new URLSearchParams({ uri: resource.uri });
            const result = await api(`${instancePath("/resources", instance)}?${query}`);
            preview.replaceChildren();
            for (const content of result?.contents ?? []) {
              if (typeof content.text === "string") preview.append(element("pre", "admin-pre", content.text));
            }
            const original = element("details");
            original.append(element("summary", "", "Complete resource JSON"), json(result));
            preview.append(original);
          } catch (error) {
            preview.replaceChildren(element("p", "admin-error", error.message));
          } finally { read.disabled = false; }
        });
        details.append(read, preview);
      }
      group.append(details);
    }
    section.append(group);
  }
  body.append(section);
}

/** Require a deliberate submit with literal JSON object arguments for every admin command. */
function commandForm(tools) {
  const form = element("form", "admin-command-form");
  form.append(element("h3", "", "Execute a tool on this instance"));
  const select = element("select");
  select.setAttribute("aria-label", "Tool name");
  for (const tool of tools) {
    const option = element("option", "", tool.name);
    option.value = tool.name;
    select.append(option);
  }
  if (!tools.some((tool) => tool.name === state.toolName)) state.toolName = tools[0]?.name ?? "";
  select.value = state.toolName;
  select.addEventListener("change", () => { state.toolName = select.value; });
  const argumentsInput = element("textarea");
  argumentsInput.spellcheck = false;
  argumentsInput.value = state.arguments;
  argumentsInput.addEventListener("input", () => { state.arguments = argumentsInput.value; });
  form.append(element("label", "", "Tool", [select]), element("label", "", "Arguments (JSON object)", [argumentsInput]));
  const submit = element("button", "", state.executing ? "Executing…" : "Execute command");
  submit.type = "submit";
  submit.disabled = state.executing || !state.snapshot.instance?.connected || !tools.length;
  form.append(submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.controls || state.executing) return;
    let args;
    try {
      args = JSON.parse(argumentsInput.value);
      if (!args || typeof args !== "object" || Array.isArray(args)) throw new Error("Arguments must be a JSON object.");
    } catch (error) { inform(`Invalid arguments: ${error.message}`); return; }
    const instance = state.instanceId;
    const selection = selectionGeneration;
    state.executing = true;
    submit.disabled = true;
    submit.textContent = "Executing…";
    inform(null);
    try {
      await api(instancePath(`/tools/${encodeURIComponent(select.value)}`, instance), { method: "POST", value: { arguments: args } });
      if (selection === selectionGeneration) {
        state.page = "history";
        state.callId = null;
        renderPageTabs();
        await refreshSelected();
      }
    } catch (error) { if (selection === selectionGeneration) inform(error.message); }
    finally {
      state.executing = false;
      if (selection === selectionGeneration && state.page === "catalog") renderPage();
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
    const content = element("textarea");
    content.spellcheck = false;
    content.value = "{}";
    if (!urlMode) form.append(element("label", "", "Response content (JSON object)", [content]));
    const actions = element("div", "admin-result-tabs");
    for (const action of ["accept", "decline", "cancel"]) {
      actions.append(button(action[0].toUpperCase() + action.slice(1), async () => {
        let result = { action };
        if (action === "accept" && !urlMode) {
          try {
            const value = JSON.parse(content.value);
            if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Response content must be an object.");
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
