import MarkdownIt from "markdown-it";
import { appendSourceText, cppSyntax } from "./source-view.js";
import { documentOutline, navigationView } from "./clangd-navigation-view.js";

// Render parser-generated markup only. Links/images do not navigate or load resources.
const markdown = new MarkdownIt({ html: false, linkify: false });
markdown.renderer.rules.link_open = () => '<span class="fm-markdown-link">';
markdown.renderer.rules.link_close = () => "</span>";
markdown.renderer.rules.image = (tokens, index) => markdown.utils.escapeHtml(tokens[index].content);

function formatRange(range) {
  const position = (value) => `${value.line}:${value.character + 1}`;
  return range.start.line === range.end.line && range.start.character === range.end.character
    ? position(range.start) : `${position(range.start)}–${position(range.end)}`;
}

function codeContent(doc, code, language = "cpp") {
  const text = code.textContent;
  const lines = text.split(/(?<=\n)|(?<=\r)(?!\n)/);
  const cpp = /^(?:c|cpp|c\+\+|cc|cxx|h|hpp)$/i.test(language);
  if (!cpp) return;
  const tokens = cppSyntax(lines, language === "c" ? "snippet.c" : "snippet.cpp");
  code.replaceChildren();
  lines.forEach((line, index) => appendSourceText(doc, code, line, "", undefined, index + 1, undefined, [], tokens[index]));
}

function markdownContent(doc, text) {
  const content = doc.createElement("div");
  content.className = "fm-markdown fm-syntax";
  content.innerHTML = markdown.render(text);
  for (const code of content.querySelectorAll("pre > code")) {
    const language = [...code.classList].find((name) => name.startsWith("language-"))?.slice(9) || "cpp";
    code.parentElement.classList.add("fm-hover-code");
    codeContent(doc, code, language);
  }
  return content;
}

/** All configuration answers remain visible; JSON/copy use the original result. */
export function clangdPresentation(data, resources, toolName) {
  const records = Array.isArray(data) ? data : Array.isArray(data?.result) ? data.result : null;
  return {
    toolName,
    summary: records === null ? data : Array.isArray(data) ? {} : Object.fromEntries(Object.entries(data).filter(([key]) => key !== "result")),
    records,
    titleKey: toolName === "clangd_configurations" ? "id" : "path",
    recordLabel: {
      clangd_definition: "Definitions", clangd_references: "References", clangd_workspace_symbols: "Workspace symbols",
    }[toolName],
    filterPlaceholder: "Filter configurations and results",
    unlabeledFields: ["diagnostics", "hover", "locations", "symbols"],
  };
}

export function clangdValue(value, key, record, { doc, valueNode, query = "", toolName }) {
  const node = (tag, className, text) => {
    const result = doc.createElement(tag);
    result.className = className;
    if (text !== undefined) result.textContent = text;
    return result;
  };
  const localQuery = record?.path?.toLocaleLowerCase().includes(query) || record?.configurations?.some((id) => id.toLocaleLowerCase().includes(query)) ? "" : query;
  if (key === "locations" && Array.isArray(value)) return navigationView(doc, value, localQuery, toolName === "clangd_definition" ? "Definitions" : "References");
  if (key === "symbols" && Array.isArray(value)) {
    if (record?.path) return documentOutline(doc, value, localQuery);
    return navigationView(doc, value.map((symbol) => ({
      ...symbol.location, name: symbol.name, kind: symbol.kind, container: symbol.container_name, tags: symbol.tags,
    })), localQuery, "Symbols");
  }
  if (key === "configurations" && Array.isArray(value)) {
    const list = node("span", "fm-configuration-list");
    for (const id of value) list.append(node("span", "fm-configuration-badge", id));
    return list;
  }
  if (key === "position" && value) return node("span", "fm-location", `${value.line}:${value.character + 1}`);
  if (key === "hover") {
    const card = node("div", "fm-source fm-hover-card");
    if (!value) { card.append(node("p", "fm-label", "No symbol information at this position.")); return card; }
    for (const item of value.contents) {
      if (item.kind === "markdown") card.append(markdownContent(doc, item.text));
      else if (item.kind === "code") {
        const pre = node("pre", "fm-hover-code fm-syntax");
        const code = node("code", "", item.text);
        codeContent(doc, code, item.language);
        pre.append(code);
        card.append(pre);
      } else card.append(node("p", "fm-hover-message", item.text));
    }
    if (!value.contents.length) card.append(node("p", "fm-label", "No symbol information at this position."));
    if (value.range) card.append(node("div", "fm-hover-range", `Range ${formatRange(value.range)}`));
    return card;
  }
  if (key === "severity") {
    const node = doc.createElement("span");
    node.className = value === "error" ? "fm-error" : value === "warning" ? "fm-warning" : "fm-label";
    node.textContent = value ?? "null";
    return node;
  }
  if (key === "diagnostics" && Array.isArray(value) && value.every((item) => typeof item?.message === "string")) {
    const list = node("div", "fm-source fm-diagnostics");
    if (!value.length) {
      list.append(node("div", "fm-diagnostic-clean fm-success", "✓ No diagnostics"));
      return list;
    }
    const counts = new Map();
    for (const diagnostic of value) counts.set(diagnostic.severity ?? "diagnostic", (counts.get(diagnostic.severity ?? "diagnostic") ?? 0) + 1);
    const summary = node("div", "fm-diagnostic-counts");
    for (const [severity, count] of counts) {
      const label = severity === "information" ? "note" : severity;
      summary.append(node("span", `fm-severity fm-severity-${severity}`, `${count} ${label}${count === 1 ? "" : "s"}`));
    }
    list.append(summary);
    for (const diagnostic of value) {
      const severity = diagnostic.severity ?? "diagnostic";
      const card = node("article", `fm-diagnostic-card fm-severity-${severity}`);
      const heading = node("header", "fm-diagnostic-heading");
      heading.append(node("span", `fm-severity fm-severity-${severity}`, severity), node("span", "fm-location", formatRange(diagnostic.range)));
      const origin = [diagnostic.source, diagnostic.code].filter((item) => item !== null && item !== undefined).join(" · ");
      if (origin) heading.append(node("span", "fm-diagnostic-origin", origin));
      card.append(heading, node("div", "fm-hover-message", diagnostic.message));
      for (const related of diagnostic.related ?? []) {
        const note = node("div", "fm-diagnostic-note");
        note.append(node("div", "fm-hover-location", `${related.location.path} · ${formatRange(related.location.range)}`), node("div", "fm-hover-message", related.message));
        card.append(note);
      }
      for (const tag of diagnostic.tags ?? []) card.append(node("span", "fm-diagnostic-tag", { 1: "Unnecessary", 2: "Deprecated" }[tag] ?? `Tag ${tag}`));
      list.append(card);
    }
    return list;
  }
  if (key === "analysis" && Array.isArray(value)) {
    const list = doc.createElement("div");
    list.className = "fm-source fm-analysis";
    for (const file of value) {
      const heading = doc.createElement("h2");
      heading.className = "fm-record-heading";
      heading.textContent = file.path;
      list.append(heading, valueNode(file.diagnostics, "diagnostic_groups"));
    }
    if (!value.length) list.textContent = "No analyzed files";
    return list;
  }
  return undefined;
}

