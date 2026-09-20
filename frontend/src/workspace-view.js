/** Workspace snapshot projections and file/tree values. No transport or SDK. */
import { isObject } from "./shared/presentation.js";

export function workspacePresentation(data) {
  if (!isObject(data)) return { toolName: "Workspace result", summary: data, records: null };
  if (Array.isArray(data.paths)) {
    const { paths, ...summary } = data;
    return { toolName: "workspace_find_files", summary, records: paths.map((path) => ({ path })), titleKey: "path" };
  }
  if (Array.isArray(data.matches)) {
    const { matches, ...summary } = data;
    return { toolName: "workspace_search", summary, records: matches, titleKey: "path" };
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
    const lines = value.split(/(?<=\n)|(?<=\r)(?!\n)/);
    lines.forEach((text, index) => {
      if (index === lines.length - 1 && text === "") return;
      const line = doc.createElement("div");
      line.className = "fm-source-line";
      line.dataset.line = String((record.start_line ?? record.line) + index);
      const code = doc.createElement("code");
      code.textContent = text;
      line.append(code);
      source.append(line);
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
    kind.textContent = ` · ${entry.kind}`;
    item.append(label, kind);
    for (const [name, field] of Object.entries(entry)) {
      if (["path", "kind"].includes(name)) continue;
      if (name === "children" && Array.isArray(field)) {
        item.append(workspaceValue(field, "entries", entry, { doc, valueNode }));
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
