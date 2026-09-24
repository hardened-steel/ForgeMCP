/** Workspace snapshot projections and file/tree values. No transport or SDK. */
import { isObject } from "./shared/presentation.js";

function searchView(doc, data, query, width) {
  const node = (tag, className, text) => {
    const result = doc.createElement(tag);
    result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  };
  const view = node("div", "fm-search-results");
  const tabs = node("div", "fm-search-tabs");
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Search results");
  const matches = node("div", "fm-search-matches");
  const skipped = node("div", "fm-search-skipped");
  const panels = [matches, skipped];
  const buttons = ["Matches", `Skipped files (${data.skipped_files?.length ?? 0})`].map((title, index) => {
    const button = node("button", "", title);
    button.type = "button";
    button.id = `workspace-search-tab-${index}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", `workspace-search-panel-${index}`);
    panels[index].id = `workspace-search-panel-${index}`;
    panels[index].setAttribute("role", "tabpanel");
    panels[index].setAttribute("aria-labelledby", button.id);
    button.addEventListener("click", () => select(index));
    button.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === "Home" ? 0 : event.key === "End" ? 1 : 1 - index;
      select(target);
      buttons[target].focus();
    });
    tabs.append(button);
    return button;
  });
  function select(index) {
    buttons.forEach((button, i) => {
      button.setAttribute("aria-selected", String(i === index));
      button.tabIndex = i === index ? 0 : -1;
      panels[i].hidden = i !== index;
    });
  }
  select(0);
  const groups = new Map();
  for (const match of data.matches) {
    if (!`${match.path}\n${match.text}`.toLocaleLowerCase().includes(query)) continue;
    if (!groups.has(match.path)) groups.set(match.path, []);
    groups.get(match.path).push(match);
  }
  // Measure the host font so tabs, wide Unicode glyphs, and larger fonts still fit.
  const canvas = width > 0 ? doc.createElement("canvas").getContext("2d") : null;
  if (canvas) {
    const style = doc.defaultView.getComputedStyle(doc.querySelector(".fm-widget"));
    canvas.font = `${style.fontSize} ${style.fontFamily}`;
  }
  const measure = (text) => {
    const expanded = text.replaceAll("\t", "    ");
    return canvas ? canvas.measureText(expanded).width : expanded.length * 8;
  };
  const available = Math.max(32, (width || 800) - measure("000000") - 42);
  const budget = Math.max(4, Math.floor(available / measure("M")));
  for (const [path, items] of groups) {
    const file = node("details", "fm-search-file");
    file.open = true;
    file.append(node("summary", "", `${path} · ${items.length}`));
    for (const match of items) {
      const points = Array.from(match.text);
      const spans = match.spans ?? [];
      const anchor = spans[0]?.[0] ?? 0;
      let start = 0;
      let end = points.length;
      if (measure(match.text) > available) {
        start = Math.max(0, Math.min(anchor - Math.floor(budget / 3), points.length - budget));
        end = Math.min(points.length, start + budget);
        const preview = () => `${start > 0 ? "…" : ""}${points.slice(start, end).join("")}${end < points.length ? "…" : ""}`;
        while (end - start > 1 && measure(preview()) > available) {
          if (anchor - start > (end - start) / 3) start++;
          else end--;
        }
      }
      const truncated = start > 0 || end < points.length;
      const row = node(truncated ? "button" : "div", "fm-match-line");
      row.dataset.line = String(match.line);
      if (truncated) {
        row.type = "button";
        row.setAttribute("aria-expanded", "false");
        row.setAttribute("aria-label", `Expand line ${match.line}`);
      }
      const code = node("code", "");
      row.append(code);
      let expanded = false;
      function renderLine() {
        code.replaceChildren();
        const from = expanded ? 0 : start;
        const to = expanded ? points.length : end;
        if (from > 0) code.append(doc.createTextNode("…"));
        let cursor = from;
        for (const [a, b] of spans) {
          if (b < from || a > to) continue;
          const left = Math.max(from, a, cursor);
          const right = Math.min(to, b);
          if (right < left) continue;
          appendHighlighted(doc, code, points.slice(cursor, left).join(""), query);
          const mark = node("mark", a === b ? "fm-zero-match" : "");
          appendHighlighted(doc, mark, points.slice(left, right).join(""), query);
          if (a === b) mark.setAttribute("aria-label", "Zero-width match");
          code.append(mark);
          cursor = right;
        }
        appendHighlighted(doc, code, points.slice(cursor, to).join(""), query);
        if (to < points.length) code.append(doc.createTextNode("…"));
      }
      if (truncated) row.addEventListener("click", () => {
        expanded = !expanded;
        row.classList.toggle("fm-match-expanded", expanded);
        row.setAttribute("aria-expanded", String(expanded));
        row.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} line ${match.line}`);
        renderLine();
      });
      renderLine();
      file.append(row);
    }
    matches.append(file);
  }
  if (!groups.size) matches.append(node("p", "fm-empty", "No matching lines."));
  const paths = (data.skipped_files ?? []).filter((path) => path.toLocaleLowerCase().includes(query));
  for (const path of paths) skipped.append(node("div", "fm-skipped-path", path));
  if (!paths.length) skipped.append(node("p", "fm-empty", "No skipped files."));
  view.append(tabs, matches, skipped);
  return view;
}

function appendHighlighted(doc, target, text, query) {
  if (!query) { target.append(doc.createTextNode(text)); return; }
  let cursor = 0;
  const lower = text.toLocaleLowerCase();
  while (cursor < text.length) {
    const found = lower.indexOf(query, cursor);
    if (found < 0) { target.append(doc.createTextNode(text.slice(cursor))); break; }
    target.append(doc.createTextNode(text.slice(cursor, found)));
    const mark = doc.createElement("mark");
    mark.className = "fm-local-match";
    mark.textContent = text.slice(found, found + query.length);
    target.append(mark);
    cursor = found + query.length;
  }
}

function resources(data) {
  return ["extensions_uri", "resources"].filter((key) => key in data);
}

function without(data, keys) {
  return Object.fromEntries(Object.entries(data).filter(([key]) => !keys.includes(key)));
}

function findView(doc, data, query, state) {
  const container = doc.createElement("div");
  const controls = doc.createElement("div");
  controls.className = "fm-file-sort";
  for (const [key, label] of [["name", "Name"], ["modified_at", "Date"], ["size_bytes", "Size"]]) {
    const control = doc.createElement("button");
    control.type = "button";
    control.textContent = label + (state.key === key ? (state.desc ? " ↓" : " ↑") : "");
    control.setAttribute("aria-pressed", String(state.key === key));
    control.addEventListener("click", () => {
      state.desc = state.key === key ? !state.desc : key !== "name";
      state.key = key;
      container.replaceWith(findView(doc, data, query, state));
    });
    controls.append(control);
  }
  container.append(controls);
  const metadata = new Map((data.files ?? []).map((file) => [file.path, file]));
  const paths = data.paths.filter((path) => path.toLocaleLowerCase().includes(query));
  paths.sort((left, right) => {
    const a = state.key === "name" ? left : metadata.get(left)?.[state.key];
    const b = state.key === "name" ? right : metadata.get(right)?.[state.key];
    const compared = typeof a === "number" && typeof b === "number" ? a - b
      : String(a ?? "").localeCompare(String(b ?? ""));
    return (state.desc ? -compared : compared) || left.localeCompare(right);
  });
  for (const path of paths) {
    const row = doc.createElement("div");
    row.className = "fm-file-path";
    row.textContent = path;
    container.append(row);
  }
  if (!paths.length) {
    const empty = doc.createElement("p");
    empty.className = "fm-empty";
    empty.textContent = "No matching files.";
    container.append(empty);
  }
  return container;
}

export function workspacePresentation(data) {
  if (!isObject(data)) return { toolName: "Workspace result", summary: data, records: null };
  if (Array.isArray(data.paths)) {
    const state = { key: "name", desc: false };
    return {
      toolName: "workspace_find_files", summary: data, records: null,
      resourceFields: resources(data), filterPlaceholder: "Filter files",
      render: (doc, query) => findView(doc, data, query, state),
      count: `${data.paths.length} files`,
    };
  }
  if (Array.isArray(data.matches)) {
    return {
      toolName: "workspace_search", summary: data, records: null,
      resourceFields: resources(data), filterPlaceholder: "Search results",
      count: `${data.matches.length} matches · ${new Set(data.matches.map((match) => match.path)).size} files`,
      render: (doc, query, width) => searchView(doc, data, query, width),
    };
  }
  const toolName = Array.isArray(data.entries) ? "workspace_list"
    : "start_line" in data ? "workspace_read_file"
    : "size_bytes" in data ? "workspace_file_info"
    : "lines_added" in data ? "workspace_write_file"
    : "replacements" in data ? "workspace_edit_file"
    : data.action === "moved" ? "workspace_move"
    : data.action === "deleted" ? "workspace_delete"
    : ["created", "already_exists"].includes(data.action) ? "workspace_mkdir" : "Workspace result";
  const resourceFields = resources(data);
  const hidden = [...resourceFields];
  if (toolName === "workspace_read_file") hidden.push("start_line");
  if (toolName === "workspace_edit_file") hidden.push("changed_lines");
  if (toolName === "workspace_move" || toolName === "workspace_delete") {
    const fields = toolName === "workspace_move" ? ["source", "path"] : ["path"];
    return {
      toolName, summary: data, records: null, resourceFields,
      viewData: Object.fromEntries(fields.map((key) => [key === "path" && toolName === "workspace_move" ? "destination" : key, data[key]])),
      fieldFilter: false, hideFilter: true, compact: true,
    };
  }
  if (toolName === "workspace_list") {
    return {
      toolName, summary: data, records: null, resourceFields,
      viewData: without(data, hidden), fieldFilter: false, filterPlaceholder: "Filter files",
    };
  }
  if (["workspace_read_file", "workspace_write_file", "workspace_edit_file"].includes(toolName)) {
    const display = without(data, hidden);
    if (toolName === "workspace_edit_file") display.replacements = data.changed_lines?.join(", ") || "None";
    return {
      toolName, summary: data, records: null, resourceFields, viewData: display,
      fieldFilter: false, filterPlaceholder: "Search lines",
    };
  }
  return { toolName, summary: data, records: null, resourceFields, viewData: without(data, hidden) };
}

export function workspaceValue(value, key, record, { doc, valueNode, query = "" }) {
  if ((key === "text" || key === "diff") && typeof value === "string") {
    const source = doc.createElement("div");
    source.className = "fm-source";
    source.setAttribute("aria-label", key === "diff" ? "File diff with line numbers" : "File text with line numbers");
    if (!value) { source.textContent = "Empty string"; return source; }
    const wrap = doc.createElement("button");
    wrap.type = "button";
    wrap.textContent = "Wrap lines";
    wrap.setAttribute("aria-pressed", "true");
    wrap.addEventListener("click", () => {
      const enabled = wrap.getAttribute("aria-pressed") !== "true";
      wrap.setAttribute("aria-pressed", String(enabled));
      source.classList.toggle("fm-source-nowrap", !enabled);
    });
    const viewport = doc.createElement("div");
    viewport.className = "fm-source-viewport";
    viewport.tabIndex = 0;
    viewport.setAttribute("aria-label", "File contents");
    source.append(wrap, viewport);
    const lines = value.split(/(?<=\n)|(?<=\r)(?!\n)/);
    let oldLine = 0;
    let newLine = 0;
    lines.forEach((text, index) => {
      if (index === lines.length - 1 && text === "") return;
      let number = String((record.start_line ?? record.line ?? 1) + index);
      if (key === "diff") {
        const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(text);
        if (hunk) {
          oldLine = Number(hunk[1]);
          newLine = Number(hunk[2]);
          number = "";
        } else if (text.startsWith("---") || text.startsWith("+++")) {
          number = "";
        } else if (text.startsWith("-")) {
          number = `-${oldLine++}`;
        } else if (text.startsWith("+")) {
          number = `+${newLine++}`;
        } else if (oldLine && newLine) {
          number = String(newLine++);
          oldLine++;
        }
      }
      if (query && !text.toLocaleLowerCase().includes(query)) return;
      const line = doc.createElement("div");
      line.className = "fm-source-line";
      line.dataset.line = number;
      if (key === "diff") {
        if (text.startsWith("+")) line.classList.add("fm-diff-add");
        if (text.startsWith("-")) line.classList.add("fm-diff-remove");
      }
      const code = doc.createElement("code");
      appendHighlighted(doc, code, text, query);
      line.append(code);
      viewport.append(line);
    });
    if (query && !viewport.children.length) {
      const empty = doc.createElement("p");
      empty.className = "fm-empty";
      empty.textContent = "No matching lines.";
      viewport.append(empty);
    }
    return source;
  }
  if (key !== "entries" || !Array.isArray(value)) return undefined;
  const tree = doc.createElement("ul");
  tree.className = "fm-tree";
  if (!value.length) {
    const empty = doc.createElement("li");
    empty.textContent = "Empty list";
    tree.append(empty);
  }
  const visible = (entry) => {
    if (!query) return true;
    if (entry?.kind === "directory") {
      return entry.children?.some(visible) ?? false;
    }
    return String(entry?.path ?? "").toLocaleLowerCase().includes(query);
  };
  const shown = value.filter(visible);
  if (query && !shown.length) {
    const empty = doc.createElement("li");
    empty.className = "fm-empty";
    empty.textContent = "No matching files.";
    tree.append(empty);
  }
  for (const entry of shown) {
    const item = doc.createElement("li");
    if (!isObject(entry)) { item.append(valueNode(entry)); tree.append(item); continue; }
    const label = doc.createElement("span");
    label.className = "fm-tree-path";
    label.textContent = String(entry.path).split("/").at(-1);
    const kind = doc.createElement("span");
    kind.className = "fm-label";
    const empty = entry.kind === "directory" && Array.isArray(entry.children) && entry.children.length === 0;
    kind.textContent = ` · ${empty ? "empty directory" : entry.kind}`;
    item.append(label, kind);
    for (const [name, field] of Object.entries(entry)) {
      if (["path", "kind"].includes(name)) continue;
      if (name === "children") {
        if (Array.isArray(field) && field.length) {
          item.append(workspaceValue(field, "entries", entry, { doc, valueNode, query }));
        }
      } else {
        const extra = doc.createElement("span");
        extra.className = "fm-tree-extra";
        const nameNode = doc.createElement("span");
        nameNode.className = "fm-label";
        nameNode.textContent = ` ${name}: `;
        extra.append(nameNode, valueNode(field, name, entry));
        item.append(extra);
      }
    }
    tree.append(item);
  }
  return tree;
}
