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
          code.append(doc.createTextNode(points.slice(cursor, left).join("")));
          const mark = node("mark", a === b ? "fm-zero-match" : "", points.slice(left, right).join(""));
          if (a === b) mark.setAttribute("aria-label", "Zero-width match");
          code.append(mark);
          cursor = right;
        }
        code.append(doc.createTextNode(points.slice(cursor, to).join("")));
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

export function workspacePresentation(data) {
  if (!isObject(data)) return { toolName: "Workspace result", summary: data, records: null };
  if (Array.isArray(data.paths)) {
    const { paths, ...summary } = data;
    return { toolName: "workspace_find_files", summary, records: paths.map((path) => ({ path })), titleKey: "path" };
  }
  if (Array.isArray(data.matches)) {
    return {
      toolName: "workspace_search", summary: data, records: null,
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
  return { toolName, summary: data, records: null };
}

export function workspaceValue(value, key, record, { doc, valueNode }) {
  if (key === "text" && typeof value === "string" && ("start_line" in record || "line" in record)) {
    const source = doc.createElement("div");
    source.className = "fm-source";
    source.setAttribute("aria-label", "File text with line numbers");
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
    lines.forEach((text, index) => {
      if (index === lines.length - 1 && text === "") return;
      const line = doc.createElement("div");
      line.className = "fm-source-line";
      line.dataset.line = String((record.start_line ?? record.line) + index);
      const code = doc.createElement("code");
      code.textContent = text;
      line.append(code);
      viewport.append(line);
    });
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
  for (const entry of value) {
    const item = doc.createElement("li");
    if (!isObject(entry)) { item.append(valueNode(entry)); tree.append(item); continue; }
    const label = doc.createElement("span");
    label.className = "fm-tree-path";
    label.textContent = entry.path;
    const kind = doc.createElement("span");
    kind.className = "fm-label";
    const empty = entry.kind === "directory" && Array.isArray(entry.children) && entry.children.length === 0;
    kind.textContent = ` · ${empty ? "empty directory" : entry.kind}`;
    item.append(label, kind);
    for (const [name, field] of Object.entries(entry)) {
      if (["path", "kind"].includes(name)) continue;
      if (name === "children") {
        if (Array.isArray(field) && field.length) {
          item.append(workspaceValue(field, "entries", entry, { doc, valueNode }));
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
