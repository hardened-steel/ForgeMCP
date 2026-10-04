/** Local navigation through captured locations and symbol outlines. No file requests. */
import { appendSourceText, cppSyntax } from "./source-view.js";

const kinds = ["Symbol", "File", "Module", "Namespace", "Package", "Class", "Method", "Property", "Field", "Constructor", "Enum", "Interface", "Function", "Variable", "Constant", "String", "Number", "Boolean", "Array", "Object", "Key", "Null", "Enum member", "Struct", "Event", "Operator", "Type parameter"];
const symbolKind = (kind) => kinds[kind] ?? `Kind ${kind}`;
const tone = (kind) => [6, 9, 12, 25].includes(kind) ? "function" : [2, 3, 5, 10, 11, 23, 26].includes(kind) ? "type" : "variable";
const node = (doc, tag, className, text) => {
  const result = doc.createElement(tag);
  result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
};
const coordinates = (range) => `${range.start.line}:${range.start.character + 1}–${range.end.line}:${range.end.character + 1}`;

function kindBadge(doc, kind) {
  return node(doc, "span", `fm-symbol-kind fm-symbol-${tone(kind)}`, symbolKind(kind));
}

/** Left source preview, right file groups. Selection only switches saved excerpts. */
export function navigationView(doc, entries, query = "", label = "Locations") {
  const view = node(doc, "div", "fm-source fm-navigation");
  const header = node(doc, "div", "fm-navigation-heading");
  const layout = node(doc, "div", "fm-navigation-layout");
  const preview = node(doc, "div", "fm-nav-preview");
  const index = node(doc, "div", "fm-nav-index");
  index.setAttribute("aria-label", label);
  const visible = query ? entries.filter((entry) => [entry.path, entry.name, entry.container, entry.preview?.text].filter(Boolean).join("\n").toLocaleLowerCase().includes(query)) : entries;
  const files = new Map();
  for (const entry of visible) {
    if (!files.has(entry.path)) files.set(entry.path, []);
    files.get(entry.path).push(entry);
  }
  header.append(node(doc, "span", "", `${visible.length} ${label.toLocaleLowerCase()} · ${files.size} files`));
  view.append(header);
  if (!visible.length) {
    view.append(node(doc, "p", "fm-empty", query ? "No matching locations." : `No ${label.toLocaleLowerCase()} found.`));
    return view;
  }
  let active;
  const buttons = new Map();
  function select(entry) {
    active = entry;
    for (const [item, button] of buttons) button.setAttribute("aria-pressed", String(item === entry));
    preview.replaceChildren();
    const heading = node(doc, "div", "fm-nav-target-heading");
    heading.append(node(doc, "span", "fm-nav-path", `${entry.path}:${entry.range.start.line}`));
    if (entry.name) heading.append(node(doc, "strong", `fm-symbol-name fm-symbol-${tone(entry.kind)}`, entry.name));
    heading.append(node(doc, "span", "fm-label", `Range ${coordinates(entry.range)}`));
    preview.append(heading);
    if (!entry.preview) {
      preview.append(node(doc, "p", "fm-empty", entry.path.startsWith("root/") ? "External file · no source preview" : "Source preview unavailable in this result."));
      return;
    }
    const source = node(doc, "div", "fm-source-viewport fm-location-preview fm-syntax");
    source.tabIndex = 0;
    source.setAttribute("aria-label", `Source at ${entry.path}:${entry.range.start.line}`);
    const lines = entry.preview.text.split(/(?<=\n)|(?<=\r)(?!\n)/);
    const syntax = cppSyntax(lines, entry.path);
    lines.forEach((text, offset) => {
      if (!text && offset === lines.length - 1) return;
      const number = entry.preview.start_line + offset;
      const row = node(doc, "div", "fm-source-line");
      row.dataset.line = String(number);
      row.classList.toggle("fm-nav-target-line", number === entry.range.start.line);
      const code = node(doc, "code", "");
      const range = entry.range;
      const changes = [];
      if (range.start.line <= number && range.end.line >= number && !(range.end.line === number && range.end.character === 0 && range.start.line !== number)) {
        changes.push([
          range.start.line === number ? range.start.character : 0,
          range.end.line === number ? range.end.character : Array.from(text.replace(/[\r\n]+$/, "")).length,
        ]);
      }
      appendSourceText(doc, code, text, "", undefined, number, undefined, changes, syntax[offset]);
      row.append(code);
      source.append(row);
    });
    preview.append(source);
  }
  function entryButton(entry) {
    const button = node(doc, "button", "fm-nav-entry");
    button.type = "button";
    button.setAttribute("aria-label", `${entry.name ? `${entry.name} · ` : ""}${entry.path}:${entry.range.start.line}:${entry.range.start.character + 1}`);
    button.setAttribute("aria-pressed", String(entry === active));
    const title = node(doc, "span", "fm-nav-entry-heading");
    title.append(node(doc, "span", "fm-nav-line-number", String(entry.range.start.line)));
    if (entry.name) title.append(kindBadge(doc, entry.kind), node(doc, "span", `fm-symbol-name fm-symbol-${tone(entry.kind)}`, entry.name));
    else {
      const line = entry.preview?.text.split(/\r\n|\n|\r/)[entry.range.start.line - entry.preview.start_line];
      if (line !== undefined) {
        const points = Array.from(line);
        const start = Math.max(0, entry.range.start.character - 45);
        const end = Math.min(points.length, start + 160);
        const code = node(doc, "code", "fm-nav-line-code");
        if (start) code.append(doc.createTextNode("…"));
        const right = entry.range.end.line === entry.range.start.line ? entry.range.end.character : points.length;
        appendSourceText(doc, code, points.slice(start, end).join(""), "", undefined, undefined, undefined, [[entry.range.start.character - start, right - start]]);
        if (end < points.length) code.append(doc.createTextNode("…"));
        title.append(code);
      } else title.append(node(doc, "span", "fm-label", `Column ${entry.range.start.character + 1}`));
    }
    button.append(title);
    if (entry.tags?.includes(1)) button.classList.add("fm-deprecated");
    button.addEventListener("click", () => select(entry));
    buttons.set(entry, button);
    return button;
  }
  for (const [path, items] of files) {
    const group = node(doc, "details", "fm-nav-file");
    group.open = files.size === 1 || path === visible[0].path;
    const summary = node(doc, "summary", "fm-nav-file-heading");
    summary.append(node(doc, "span", "fm-nav-file-path", path), node(doc, "span", "fm-navigation-count", String(items.length)));
    group.append(summary);
    // Closed files retain their data but create rows only when opened.
    let built = false;
    const build = () => {
      if (!group.open || built) return;
      built = true;
      const scopes = new Map();
      for (const entry of items) {
        const scope = entry.container ?? "";
        if (!scopes.has(scope)) scopes.set(scope, []);
        scopes.get(scope).push(entry);
      }
      for (const [scope, members] of scopes) {
        if (scope) group.append(node(doc, "div", "fm-symbol-scope", scope));
        for (const entry of members) group.append(entryButton(entry));
      }
    };
    group.addEventListener("toggle", build);
    build();
    index.append(group);
  }
  layout.append(preview, index);
  view.append(layout);
  select(visible[0]);
  return view;
}

/** A document outline preserves clangd's nesting, signatures, and exact ranges. */
export function documentOutline(doc, symbols, query = "") {
  const filter = (items) => items.flatMap((symbol) => {
    if (!query || `${symbol.name}\n${symbol.detail ?? ""}`.toLocaleLowerCase().includes(query)) return [symbol];
    const children = filter(symbol.children ?? []);
    return children.length ? [{ ...symbol, children }] : [];
  });
  const visible = filter(symbols);
  const view = node(doc, "div", "fm-source fm-document-outline");
  const count = (items) => items.reduce((total, item) => total + 1 + count(item.children ?? []), 0);
  view.append(node(doc, "div", "fm-navigation-heading", `${count(visible)} symbols · document outline`));
  function tree(items, depth = 0) {
    const list = node(doc, "ul", "fm-symbol-tree");
    for (const symbol of items) {
      const item = node(doc, "li", "");
      const detail = node(doc, "details", "fm-outline-symbol");
      detail.open = Boolean(query) || depth < 2;
      const summary = node(doc, "summary", "fm-outline-heading");
      const name = node(doc, "span", `fm-symbol-name fm-symbol-${tone(symbol.kind)}`, symbol.name);
      if (symbol.tags?.includes(1)) name.classList.add("fm-deprecated");
      summary.append(kindBadge(doc, symbol.kind), name, node(doc, "span", "fm-outline-line", String(symbol.selection_range.start.line)));
      summary.title = `Selection ${coordinates(symbol.selection_range)}`;
      if (symbol.children?.length) summary.append(node(doc, "span", "fm-navigation-count", String(symbol.children.length)));
      detail.append(summary);
      let built = false;
      const build = () => {
        if (!detail.open || built) return;
        built = true;
        if (symbol.detail) detail.append(node(doc, "div", "fm-symbol-detail", symbol.detail));
        detail.append(node(doc, "div", "fm-symbol-range", `Range ${coordinates(symbol.range)} · selection ${coordinates(symbol.selection_range)}`));
        if (symbol.children?.length) detail.append(tree(symbol.children, depth + 1));
      };
      detail.addEventListener("toggle", build);
      build();
      item.append(detail);
      list.append(item);
    }
    return list;
  }
  if (visible.length) view.append(tree(visible));
  else view.append(node(doc, "p", "fm-empty", query ? "No matching symbols." : "No document symbols found."));
  return view;
}
