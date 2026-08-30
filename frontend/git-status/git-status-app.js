(() => {
  "use strict";

  const APP_NAME = "forgemcp-git-status";
  const APP_VERSION = "1.0.0";
  const TOOL_NAME = "git__status";
  const TABS = ["All", "Staged", "Modified", "Untracked", "Conflicted"];
  const app = document.getElementById("app");
  const state = { active: true, initialized: false, refreshing: false, tab: "All", lastSuccess: null, error: null, hostContext: {} };
  const pending = new Map();
  let nextId = 1;
  let resizeTimer = null;
  let resizeObserver = null;

  function isObject(value) { return value !== null && typeof value === "object" && !Array.isArray(value); }
  function display(value) {
    if (typeof value !== "string") return "";
    return value.replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/gu, (character) => `\\u${character.codePointAt(0).toString(16).padStart(4, "0")}`);
  }
  function integer(value, fallback = 0) { return Number.isInteger(value) && value >= 0 ? value : fallback; }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function sendNotification(method, params) {
    if (state.active) window.parent.postMessage({ jsonrpc: "2.0", method, params }, "*");
  }
  function sendRequest(method, params) {
    const id = nextId++;
    return new Promise((resolve, reject) => {
      pending.set(id, { resolve, reject });
      window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
    });
  }
  function applyHostContext(update) {
    if (!isObject(update)) return;
    state.hostContext = { ...state.hostContext, ...update };
    if (update.theme === "light" || update.theme === "dark") document.documentElement.dataset.theme = update.theme;
    const variables = update.styles && isObject(update.styles) && isObject(update.styles.variables) ? update.styles.variables : null;
    if (variables) {
      for (const [key, value] of Object.entries(variables)) {
        if (key.startsWith("--") && key.length <= 96 && typeof value === "string" && value.length <= 512) document.documentElement.style.setProperty(key, value);
      }
    }
  }
  function statusFromResult(result) {
    if (!isObject(result) || result.isError === true || !isObject(result.structuredContent)) return null;
    const status = result.structuredContent;
    if (typeof status.repository !== "string" || !Array.isArray(status.files)) return null;
    return status;
  }
  function selectedRows(status) {
    const files = status.files.filter(isObject);
    const match = (file) => {
      if (state.tab === "Staged") return file.untracked !== true && file.conflicted !== true && file.staged_status && file.staged_status !== ".";
      if (state.tab === "Modified") return file.untracked !== true && file.conflicted !== true && file.unstaged_status && file.unstaged_status !== ".";
      if (state.tab === "Untracked") return file.untracked === true;
      if (state.tab === "Conflicted") return file.conflicted === true;
      return true;
    };
    return files.filter(match);
  }
  function groupFor(file) {
    if (file.conflicted === true) return "Conflicted";
    if (file.untracked === true) return "Untracked";
    if (file.staged_status && file.staged_status !== ".") return "Staged";
    if (file.unstaged_status && file.unstaged_status !== ".") return "Modified";
    return "Other";
  }
  function addMetric(container, label, value) {
    const metric = element("section", "metric");
    metric.append(element("div", "metric-label", label), element("output", "metric-value", value));
    container.append(metric);
  }
  function addRows(container, rows) {
    if (!rows.length) { container.append(element("p", "empty", "No matching files.")); return; }
    const groups = new Map();
    for (const file of rows) {
      const group = state.tab === "All" ? groupFor(file) : state.tab;
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(file);
    }
    for (const [group, files] of groups) {
      const section = element("section", "group");
      section.append(element("h2", "group-title", group));
      const list = element("div", "rows");
      for (const file of files) {
        const code = `${display(file.staged_status || ".")}${display(file.unstaged_status || ".")}`;
        const row = element("div", "row");
        const path = display(file.path);
        const original = display(file.original_path);
        row.append(element("span", "code", code), element("span", "path", original ? `${path} <- ${original}` : path));
        list.append(row);
      }
      section.append(list);
      container.append(section);
    }
  }
  function render() {
    app.replaceChildren();
    const shell = element("div", "shell");
    const header = element("header", "header");
    header.append(element("h1", "title", "Git Status"));
    const refresh = element("button", "refresh", state.refreshing ? "Refreshing" : "Refresh");
    refresh.type = "button";
    refresh.disabled = state.refreshing || !state.active;
    refresh.addEventListener("click", refreshStatus);
    header.append(refresh);
    shell.append(header);
    if (state.error) shell.append(element("div", "warning error", state.error));
    const status = state.lastSuccess;
    if (!status) {
      shell.append(element("p", "notice", state.error ? "The last result could not be displayed." : "Loading repository status..."));
      app.append(shell);
      announceSize();
      return;
    }
    const repository = display(status.repository);
    if (repository !== "available") {
      shell.append(element("p", "notice error", `Repository ${repository || "unavailable"}${status.error ? `: ${display(status.error)}` : ""}`));
      app.append(shell);
      announceSize();
      return;
    }
    const summary = element("section", "summary");
    const branch = status.unborn === true ? "unborn" : status.detached === true ? "detached" : display(status.branch) || "unknown";
    addMetric(summary, "Branch", branch);
    addMetric(summary, "HEAD", display(status.head_oid).slice(0, 12) || "none");
    addMetric(summary, "Ahead / Behind", `${integer(status.ahead, 0)} / ${integer(status.behind, 0)}`);
    addMetric(summary, "Changes", `${integer(status.staged_count)} staged, ${integer(status.unstaged_count)} modified`);
    shell.append(summary);
    const details = element("section", "details");
    details.textContent = `${integer(status.untracked_count)} untracked | ${integer(status.conflicted_count)} conflicted`;
    shell.append(details);
    if (status.incomplete === true || status.truncated === true) shell.append(element("div", "warning", "Status is incomplete or truncated; file rows and counts may be partial."));
    if (integer(status.staged_count) + integer(status.unstaged_count) + integer(status.untracked_count) + integer(status.conflicted_count) === 0 && status.files.length === 0) shell.append(element("p", "notice clean", "Working tree clean."));
    const tabs = element("nav", "tabs");
    for (const label of TABS) {
      const tab = element("button", "tab", label);
      tab.type = "button";
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(state.tab === label));
      tab.addEventListener("click", () => { state.tab = label; render(); });
      tabs.append(tab);
    }
    shell.append(tabs);
    const groups = element("section", "groups");
    addRows(groups, selectedRows(status));
    shell.append(groups);
    app.append(shell);
    announceSize();
  }
  function applyToolResult(result) {
    const status = statusFromResult(result);
    state.refreshing = false;
    if (status) { state.lastSuccess = status; state.error = null; }
    else state.error = "Git status result was unavailable or malformed.";
    render();
  }
  function refreshStatus() {
    if (state.refreshing || !state.active) return;
    state.refreshing = true;
    state.error = null;
    render();
    sendRequest("tools/call", { name: TOOL_NAME, arguments: {} }).then(applyToolResult).catch(() => {
      state.refreshing = false;
      state.error = "Refresh failed; the last successful status is still shown.";
      render();
    });
  }
  function announceSize() {
    if (!state.active) return;
    if (resizeTimer !== null) window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(() => {
      const box = document.body.getBoundingClientRect();
      sendNotification("ui/notifications/size-changed", { width: Math.ceil(box.width), height: Math.ceil(box.height) });
    }, 80);
  }
  function teardown(id) {
    state.active = false;
    if (resizeTimer !== null) window.clearTimeout(resizeTimer);
    if (resizeObserver) resizeObserver.disconnect();
    window.removeEventListener("message", onMessage);
    for (const pendingRequest of pending.values()) pendingRequest.reject(new Error("Resource torn down."));
    pending.clear();
    if (id !== undefined && id !== null) window.parent.postMessage({ jsonrpc: "2.0", id, result: {} }, "*");
  }
  function onMessage(event) {
    if (!state.active || event.source !== window.parent || !isObject(event.data)) return;
    const message = event.data;
    if (message.id !== undefined && pending.has(message.id) && !message.method) {
      const request = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) request.reject(new Error("Host request failed.")); else request.resolve(message.result);
      return;
    }
    if (message.method === "ui/notifications/tool-input" || message.method === "ui/notifications/tool-input-partial") { return; }
    if (message.method === "ui/notifications/tool-result") { applyToolResult(message.params); return; }
    if (message.method === "ui/notifications/tool-cancelled") {
      state.refreshing = false;
      state.error = "Tool request was cancelled; the last successful status is still shown.";
      render();
      return;
    }
    if (message.method === "ui/notifications/host-context-changed") { applyHostContext(message.params); render(); return; }
    if (message.method === "ui/resource-teardown") { teardown(message.id); }
  }
  window.addEventListener("message", onMessage);
  if (typeof ResizeObserver === "function") {
    resizeObserver = new ResizeObserver(announceSize);
    resizeObserver.observe(document.body);
  }
  render();
  sendRequest("ui/initialize", {
    protocolVersion: "2026-01-26",
    appInfo: { name: APP_NAME, version: APP_VERSION },
    appCapabilities: {},
  }).then((result) => {
    if (!isObject(result)) throw new Error("Malformed initialization result.");
    applyHostContext(result.hostContext);
    state.initialized = true;
    sendNotification("ui/notifications/initialized", {});
    render();
  }).catch(() => {
    state.error = "Host initialization failed.";
    render();
  });
})();
