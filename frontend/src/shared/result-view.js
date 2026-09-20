import { copyText, isObject, jsonTokens, statusTone, timestamp } from "./presentation.js";
import { createCopyIcon } from "./copy-icon.js";

/** Shared local-only view of one result. The App/transport is deliberately not passed here. */
export function createResultView(root, { toolName, describe, renderValue }) {
  const doc = root.ownerDocument;
  const win = doc.defaultView;
  const element = (tag, className = "", text) => {
    const node = doc.createElement(tag);
    node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const button = (text, className = "") => {
    const node = element("button", className, text);
    node.type = "button";
    return node;
  };
  const icon = () => createCopyIcon(doc);
  let raw;
  let presentation;
  let mode = "fields";
  let generation = 0;
  let disposed = false;
  let manualCopy = "";
  let activeHelp;

  root.classList.add("fm-widget");
  const frame = element("div", "fm-window");
  const titlebar = element("header", "fm-titlebar");
  const title = element("h1", "fm-tool-name", toolName);
  titlebar.append(element("span", "fm-brand", "[ ForgeMCP ]"), title, element("span", "fm-mode", "RESULT / READ ONLY"));
  const toolbar = element("div", "fm-toolbar");
  const views = element("div", "fm-views");
  views.setAttribute("role", "group");
  views.setAttribute("aria-label", "Result view");
  const fieldsButton = button("[ Fields ]");
  const jsonButton = button("[ JSON ]");
  const copyAll = button(undefined, "fm-copy-all");
  copyAll.setAttribute("aria-label", "Copy entire result");
  copyAll.append(icon(), element("span", "fm-copy-all-label", "Copy all"));
  views.append(fieldsButton, jsonButton);
  toolbar.append(views, copyAll);
  const filters = element("div", "fm-filters");
  const searchLabel = element("label", "fm-search");
  const search = element("input");
  search.type = "search";
  search.placeholder = "Filter all fields";
  search.setAttribute("aria-label", "Filter all fields");
  searchLabel.append(element("span", "fm-search-label", "/"), search);
  const categories = element("select");
  categories.setAttribute("aria-label", "Filter category");
  filters.append(searchLabel, categories);
  const content = element("div", "fm-scroll");
  content.setAttribute("role", "region");
  content.setAttribute("aria-label", "Complete result");
  const footer = element("footer", "fm-footer");
  const count = element("span");
  const feedback = element("span", "fm-feedback");
  feedback.setAttribute("role", "status");
  feedback.setAttribute("aria-live", "polite");
  footer.append(count, feedback);
  const tooltip = element("div", "fm-tooltip");
  tooltip.setAttribute("role", "tooltip");
  tooltip.id = "fm-tooltip";
  tooltip.hidden = true;
  frame.append(titlebar, toolbar, filters, content, footer);
  root.replaceChildren(frame, tooltip);

  function hideHelp() {
    tooltip.hidden = true;
    activeHelp?.removeAttribute("aria-describedby");
    activeHelp = undefined;
  }
  function help(control, text) {
    const show = () => {
      hideHelp();
      activeHelp = control;
      control.setAttribute("aria-describedby", tooltip.id);
      tooltip.textContent = text;
      tooltip.hidden = false;
      const box = control.getBoundingClientRect();
      const tip = tooltip.getBoundingClientRect();
      tooltip.style.left = `${Math.max(8, Math.min(box.left, win.innerWidth - tip.width - 8))}px`;
      tooltip.style.top = `${Math.max(0, box.top - tip.height - 5)}px`;
    };
    control.addEventListener("mouseenter", show);
    control.addEventListener("focus", show);
    control.addEventListener("mouseleave", hideHelp);
    control.addEventListener("blur", hideHelp);
  }
  content.addEventListener("scroll", hideHelp);
  root.addEventListener("keydown", (event) => { if (event.key === "Escape") hideHelp(); });
  help(copyAll, "Copy the complete original result as JSON, including filtered records.");

  async function copy(text) {
    const current = generation;
    let copied = false;
    try {
      if (win.navigator.clipboard?.writeText) {
        await win.navigator.clipboard.writeText(text);
        copied = true;
      }
    } catch { /* Hosts may deny clipboard access. Use a local fallback. */ }
    if (disposed || current !== generation) return;
    if (!copied) {
      const previousFocus = doc.activeElement;
      const textarea = element("textarea", "fm-clipboard");
      textarea.value = text;
      root.append(textarea);
      try {
        textarea.select();
        copied = Boolean(doc.execCommand?.("copy"));
      } catch { /* Selection below remains available without clipboard permissions. */ }
      finally { textarea.remove(); previousFocus?.focus(); }
    }
    if (copied) feedback.textContent = "Copied";
    else {
      manualCopy = text;
      mode = "copy";
      render();
      const range = doc.createRange();
      range.selectNodeContents(content.querySelector("pre"));
      const selection = win.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      feedback.textContent = "Text selected. Press Ctrl+C / ⌘C to copy.";
    }
  }

  function valueNode(value, key = "", record) {
    const custom = renderValue?.(value, key, record, { doc, valueNode });
    if (custom) return custom;
    const date = ["started_at", "finished_at", "created_at", "modified_at"].includes(key) ? timestamp(value) : null;
    if (date) {
      const output = element("span", "fm-value");
      const time = element("time", "", date.readable);
      time.dateTime = value;
      output.append(time, element("span", "fm-iso", ` (${date.iso})`));
      return output;
    }
    if (Array.isArray(value)) {
      const output = element("span", "fm-value fm-items");
      if (!value.length) output.textContent = "Empty list";
      value.forEach((item, index) => {
        if (index) output.append(element("span", "fm-separator", ", "));
        const child = valueNode(item);
        child.classList.add("fm-item");
        output.append(child);
      });
      return output;
    }
    if (isObject(value)) {
      const output = element("span", "fm-value fm-nested");
      if (!Object.keys(value).length) output.textContent = "Empty object";
      for (const [name, item] of Object.entries(value)) {
        const entry = element("span");
        entry.append(element("span", "fm-label", `${name}: `), valueNode(item, name, value));
        output.append(entry);
      }
      return output;
    }
    let tone = typeof value === "number" ? "fm-number" : typeof value === "boolean" ? "fm-boolean" : value === null ? "fm-null" : "";
    if (key === "status") tone = statusTone(value, record);
    if (key === "return_code" && typeof value === "number") tone = value === 0 ? "fm-success" : "fm-error";
    if ((key === "scan_truncated" || key === "had_decoding_errors") && value === true) tone = "fm-warning";
    return element("span", `fm-value ${tone}`, value === "" ? "Empty string" : String(value));
  }

  function fields(value) {
    const list = element("dl", "fm-fields");
    const entries = isObject(value) ? Object.entries(value) : [["value", value]];
    for (const [key, item] of entries) {
      const row = element("div", "fm-field");
      const description = element("dd", "fm-value");
      description.append(valueNode(item, key, value));
      if (description.querySelector(".fm-source, .fm-tree")) row.classList.add("fm-wide-field");
      const copyField = button(undefined, "fm-copy");
      copyField.setAttribute("aria-label", `Copy ${key}`);
      copyField.append(icon());
      help(copyField, key === "started_at" || key === "finished_at"
        ? `Copy original ${key} with full precision.` : `Copy the complete original ${key} value.`);
      copyField.addEventListener("click", () => { void copy(copyText(item)); });
      // The action is inside dd so dl keeps valid term/description semantics.
      const action = element("dd", "fm-value");
      action.append(copyField);
      row.append(element("dt", "fm-label", key), description, action);
      list.append(row);
    }
    return list;
  }

  function render() {
    hideHelp();
    content.replaceChildren();
    fieldsButton.setAttribute("aria-pressed", String(mode === "fields"));
    jsonButton.setAttribute("aria-pressed", String(mode === "json"));
    filters.hidden = mode !== "fields" || !presentation;
    if (!presentation) return;
    if (mode === "copy") {
      content.append(element("pre", "fm-json", manualCopy));
      count.textContent = "Original value — select Fields or JSON to return";
      return;
    }
    if (mode === "json") {
      const pre = element("pre", "fm-json");
      for (const token of jsonTokens(raw)) pre.append(element("span", token.tone, token.text));
      content.append(pre);
      count.textContent = "Complete original result";
      return;
    }
    const query = search.value.toLocaleLowerCase();
    const matches = (item) => JSON.stringify(item).toLocaleLowerCase().includes(query);
    if (presentation.records !== null) {
      if (!isObject(presentation.summary) || Object.keys(presentation.summary).length) {
        const summary = element("div", "fm-summary");
        summary.append(fields(presentation.summary));
        content.append(summary);
      }
      let shown = 0;
      presentation.records.forEach((record, index) => {
        if (!matches(record) || (categories.value && record?.[presentation.categoryKey] !== categories.value)) return;
        shown++;
        const article = element("section", "fm-record");
        const heading = element("h2", "fm-record-heading");
        const label = record?.[presentation.titleKey];
        heading.append(element("span", "fm-index", String(index + 1).padStart(2, "0")),
          element("span", presentation.titleKey === "status" ? statusTone(label, record) : "", typeof label === "string" ? label : "Record"));
        article.append(heading, fields(record));
        content.append(article);
      });
      if (!shown) content.append(element("p", "fm-empty", presentation.records.length ? "No matching records. Clear or change the filters." : "No records in this result."));
      count.textContent = `${shown} / ${presentation.records.length} records${shown < presentation.records.length ? " · filter active" : ""}`;
    } else {
      const entries = isObject(raw) ? Object.entries(raw) : [["value", raw]];
      const filtered = entries.filter(([key, value]) => matches({ [key]: value }));
      content.append(fields(Object.fromEntries(filtered)));
      if (!filtered.length) content.append(element("p", "fm-empty", entries.length ? "No matching fields. Clear the filter." : "Empty result."));
      count.textContent = `${filtered.length} / ${entries.length} fields${filtered.length < entries.length ? " · filter active" : ""}`;
    }
  }

  function clear(message) {
    generation++;
    raw = undefined;
    presentation = undefined;
    manualCopy = "";
    search.value = "";
    categories.replaceChildren();
    categories.hidden = true;
    fieldsButton.disabled = jsonButton.disabled = copyAll.disabled = true;
    feedback.textContent = message;
    count.textContent = "";
    render();
  }

  function receive(result) {
    if (disposed) return;
    clear("");
    if (!result || result.structuredContent === undefined || result.isError) {
      const texts = (result?.content ?? []).filter((item) => item.type === "text").map((item) => item.text);
      const message = result?.isError ? "Tool returned an error." : "No structured result was provided.";
      content.append(element("p", "fm-empty", message));
      for (const text of texts) content.append(element("pre", "fm-json", text));
      feedback.textContent = message;
      return;
    }
    raw = result.structuredContent;
    presentation = describe(raw);
    title.textContent = presentation.toolName;
    mode = "fields";
    fieldsButton.disabled = jsonButton.disabled = copyAll.disabled = false;
    const values = presentation.categoryKey
      ? [...new Set((presentation.records ?? []).map((record) => record?.[presentation.categoryKey]).filter((value) => typeof value === "string"))]
      : [];
    const all = element("option", "", presentation.categoryKey === "status" ? "All statuses" : "All types");
    all.value = "";
    categories.append(all);
    for (const value of values) {
      const option = element("option", "", value);
      option.value = value;
      categories.append(option);
    }
    categories.hidden = !values.length;
    render();
    content.scrollTop = 0;
  }

  fieldsButton.addEventListener("click", () => { mode = "fields"; render(); });
  jsonButton.addEventListener("click", () => { mode = "json"; render(); });
  search.addEventListener("input", render);
  categories.addEventListener("change", render);
  copyAll.addEventListener("click", () => { if (presentation) void copy(JSON.stringify(raw, null, 2)); });
  clear("Waiting for tool result…");
  return {
    receive,
    pending: () => { if (!disposed) clear("Waiting for tool result…"); },
    cancelled: () => { if (!disposed) clear("Tool invocation cancelled."); },
    unavailable: () => { if (!disposed) clear("Unable to connect to the host."); },
    dispose: () => { disposed = true; clear(""); root.replaceChildren(); },
  };
}
