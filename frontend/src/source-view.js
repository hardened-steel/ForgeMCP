/** Source annotations for immutable read and diff snapshots. No server access. */
const fileIndexes = new WeakMap();
const diagnosticNodes = new WeakMap();
const colors = {
  namespace: "type", type: "type", class: "type", struct: "type", interface: "type", enum: "type", typeParameter: "type", concept: "type",
  function: "function", method: "function", macro: "macro", comment: "comment", string: "string", number: "number",
  variable: "variable", parameter: "variable", property: "variable", enumMember: "number", keyword: "keyword", operator: "operator",
};
const keywords = new Set("alignas alignof asm auto bool char char8_t char16_t char32_t class const consteval constexpr constinit decltype double enum explicit extern float friend inline int long mutable noexcept nullptr operator register requires short signed sizeof static static_assert struct template thread_local typedef typename union unsigned virtual void volatile wchar_t".split(" "));
const controls = new Set("break case catch co_await co_return co_yield concept continue default delete do else export for goto if import module namespace new private protected public return switch throw try using while".split(" "));

/** Index once per immutable file; drawing a line only visits that line's annotations. */
function annotations(file, configuration, line) {
  if (!file) return {};
  let index = fileIndexes.get(file);
  if (!index) {
    index = { highlighting: new Map(), diagnostics: new Map() };
    for (const [name, field] of [["highlighting", "spans"], ["diagnostics", "diagnostics"]]) {
      for (const group of file[name]) {
        const lines = new Map();
        for (const item of group[field]) {
          const { start, end } = item.range;
          const last = end.line > start.line && end.character === 0 ? end.line - 1 : end.line;
          for (let number = start.line; number <= last; number++) {
            if (!lines.has(number)) lines.set(number, []);
            lines.get(number).push(item);
          }
        }
        for (const id of group.configurations) index[name].set(id, lines);
      }
    }
    fileIndexes.set(file, index);
  }
  return {
    tokens: index.highlighting.get(configuration)?.get(line) ?? [],
    diagnostics: index.diagnostics.get(configuration)?.get(line) ?? [],
  };
}

/** Lexical C/C++ colors complement clangd tokens, including comments and directives. */
export function cppSyntax(lines, path) {
  if (!/\.(?:c|h|cc|hh|cpp|hpp|cxx|hxx|c\+\+|h\+\+|ipp|tpp)$/i.test(path ?? "")) return [];
  let blockComment = false;
  let rawEnd = "";
  let continuedQuote = "";
  const brackets = [];
  return lines.map((text) => {
    const spans = [];
    // LSP annotations use Unicode code points, regex offsets use UTF-16 units.
    const offsets = new Map([[0, 0]]);
    let unit = 0;
    let point = 0;
    for (const char of text) { unit += char.length; offsets.set(unit, ++point); }
    const add = (start, end, kind) => spans.push({ start: offsets.get(start), end: offsets.get(end), kind });
    const expression = /\/\*|\/\/|(?:u8|u|U|L)?R"([^ ()\\\t\r\n]{0,16})\(|(?:u8|u|U|L)?["']|\b(?:0[xX][\da-fA-F']+(?:\.[\da-fA-F']*)?(?:[pP][+-]?\d+)?|0[bB][01']+|\d[\d']*(?:\.\d[\d']*)?(?:[eE][+-]?\d+)?)[\w]*|[\p{ID_Start}_][\p{ID_Continue}]*|#\s*[a-zA-Z_]+|[{}()[\]]/gu;
    let cursor = 0;
    while (cursor < text.length) {
      if (blockComment || rawEnd || continuedQuote) {
        const endMarker = blockComment ? "*/" : rawEnd;
        let end;
        if (continuedQuote) {
          const quote = continuedQuote;
          for (end = cursor; end < text.length; end++) {
            if (text[end] === "\\") end++;
            else if (text[end] === quote) { end++; continuedQuote = ""; break; }
          }
          if (continuedQuote && !/\\(?:\r?\n|\r)$/.test(text)) continuedQuote = "";
        } else {
          const found = text.indexOf(endMarker, cursor);
          end = found < 0 ? text.length : found + endMarker.length;
          if (found >= 0) { blockComment = false; rawEnd = ""; }
        }
        end = Math.min(end, text.length);
        add(cursor, end, endMarker === "*/" ? "comment" : "string");
        cursor = end;
        continue;
      }
      expression.lastIndex = cursor;
      const match = expression.exec(text);
      if (!match) break;
      const token = match[0];
      const start = match.index;
      cursor = expression.lastIndex;
      if (token === "/*") {
        add(start, cursor, "comment");
        blockComment = true;
      } else if (token === "//") {
        add(start, text.length, "comment");
        break;
      } else if (match[1] !== undefined) {
        add(start, cursor, "string");
        rawEnd = `)${match[1]}"`;
      } else if (/["']$/.test(token)) {
        add(start, cursor, "string");
        continuedQuote = token.at(-1);
      } else if (token.startsWith("#")) {
        add(start, cursor, "control");
        if (/^#\s*(?:include|include_next|import)$/.test(token)) {
          const header = /^\s*<[^>\r\n]*>/.exec(text.slice(cursor));
          if (header) { add(cursor, cursor + header[0].length, "string"); cursor += header[0].length; }
        }
      } else if (/^[\d]/.test(token)) add(start, cursor, "number");
      else if (keywords.has(token)) add(start, cursor, "keyword");
      else if (controls.has(token)) add(start, cursor, "control");
      else if (token === "true" || token === "false") add(start, cursor, "keyword");
      else if (/^[{}()[\]]$/.test(token)) {
        if ("{([".includes(token)) {
          add(start, cursor, `bracket-${brackets.length % 3}`);
          brackets.push(token);
        } else {
          const opening = { "}": "{", ")": "(", "]": "[" }[token];
          if (brackets.at(-1) === opening) brackets.pop();
          add(start, cursor, `bracket-${brackets.length % 3}`);
        }
      } else {
        let next = cursor;
        while (next < text.length && /\s/.test(text[next])) next++;
        add(start, cursor, text[next] === "(" ? "function" : "variable");
      }
    }
    return spans;
  });
}

/** Select one configuration; lexical colors also work without a clangd resource. */
export function sourceConfiguration(doc, source, file, changed) {
  let select;
  if (file) {
    const ids = [...new Set([...file.diagnostics, ...file.highlighting].flatMap((group) => group.configurations))];
    const label = doc.createElement("label");
    label.className = "fm-source-configuration";
    label.append(doc.createTextNode("Configuration "));
    select = doc.createElement("select");
    select.setAttribute("aria-label", `Source configuration for ${file.path}`);
    for (const id of ids) {
      const option = doc.createElement("option");
      option.value = option.textContent = id;
      select.append(option);
    }
    select.addEventListener("change", changed);
    label.append(select);
    source.append(label);
  }
  const syntax = doc.createElement("button");
  syntax.type = "button";
  syntax.textContent = "Syntax highlight";
  syntax.setAttribute("aria-pressed", "true");
  source.classList.add("fm-syntax");
  syntax.addEventListener("click", () => {
    const enabled = syntax.getAttribute("aria-pressed") !== "true";
    syntax.setAttribute("aria-pressed", String(enabled));
    source.classList.toggle("fm-syntax", enabled);
  });
  source.append(syntax);
  sourceDiagnostics(source);
  return () => select?.value;
}

/** A selectable diagnostic hover stays inside the host's iframe and needs no tools. */
function sourceDiagnostics(source) {
  const doc = source.ownerDocument;
  const win = doc.defaultView;
  let popup;
  let owner;
  let events;
  let timer;
  const hide = () => {
    win.clearTimeout(timer);
    events?.abort();
    owner?.removeAttribute("aria-describedby");
    popup?.remove();
    popup = owner = undefined;
  };
  const leave = () => { win.clearTimeout(timer); timer = win.setTimeout(hide, 200); };
  const show = (event) => {
    const node = event.target.closest?.("[data-diagnostic]");
    if (!node || !source.contains(node)) return;
    win.clearTimeout(timer);
    if (node === owner) return;
    hide();
    const root = source.closest(".fm-widget");
    if (!root) return;
    root.dispatchEvent(new win.Event("fm-source-reset"));
    owner = node;
    popup = doc.createElement("div");
    popup.className = "fm-source-hover";
    popup.id = "fm-source-hover";
    popup.setAttribute("role", "tooltip");
    node.setAttribute("aria-describedby", popup.id);
    const { items, configuration } = diagnosticNodes.get(node);
    for (const diagnostic of items) {
      const section = doc.createElement("section");
      const header = doc.createElement("div");
      header.className = `fm-hover-heading ${diagnostic.severity === "error" ? "fm-error" : diagnostic.severity === "warning" ? "fm-warning" : "fm-active"}`;
      header.textContent = [diagnostic.severity ?? "Diagnostic", diagnostic.source, diagnostic.code].filter((item) => item !== null && item !== undefined).join(" · ");
      const message = doc.createElement("div");
      message.className = "fm-hover-message";
      message.textContent = diagnostic.message;
      section.append(header, message);
      for (const related of diagnostic.related ?? []) {
        const location = doc.createElement("div");
        location.className = "fm-hover-location";
        location.textContent = `${related.location.path}(${related.location.range.start.line}, ${related.location.range.start.character + 1})`;
        const detail = doc.createElement("div");
        detail.className = "fm-hover-message";
        detail.textContent = related.message;
        section.append(location, detail);
      }
      popup.append(section);
    }
    const footer = doc.createElement("div");
    footer.className = "fm-hover-configuration";
    footer.textContent = configuration;
    popup.append(footer);
    root.append(popup);
    const anchor = node.getBoundingClientRect();
    const bounds = root.getBoundingClientRect();
    popup.style.maxHeight = `${Math.max(40, bounds.height - 16)}px`;
    const box = popup.getBoundingClientRect();
    popup.style.left = `${Math.max(8, Math.min(anchor.left - bounds.left, bounds.width - box.width - 8))}px`;
    const above = anchor.top - bounds.top - box.height - 5;
    popup.style.top = `${above >= 8 ? above : Math.max(8, Math.min(anchor.bottom - bounds.top + 5, bounds.height - box.height - 8))}px`;
    popup.addEventListener("mouseenter", () => win.clearTimeout(timer));
    popup.addEventListener("mouseleave", leave);
    events = new win.AbortController();
    const options = { signal: events.signal, capture: true };
    root.addEventListener("scroll", (e) => { if (!popup?.contains(e.target)) hide(); }, options);
    root.addEventListener("click", (e) => { if (!popup?.contains(e.target) && !owner?.contains(e.target)) hide(); }, options);
    root.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); }, options);
    // New results/teardown remove the entire source; do not retain a detached popup.
    root.addEventListener("fm-source-reset", hide, options);
  };
  source.addEventListener("mouseover", show);
  source.addEventListener("focusin", show);
  source.addEventListener("mouseout", (event) => { if (owner?.contains(event.target) && !owner.contains(event.relatedTarget)) leave(); });
  source.addEventListener("focusout", (event) => { if (!popup?.contains(event.relatedTarget)) leave(); });
}

/** Compose code-point ranges for syntax, diff, local search and diagnostics. */
export function appendSourceText(doc, target, text, query, file, line, configuration, changes = [], lexical = []) {
  const points = Array.from(text);
  const intervals = [];
  const add = (start, end, className, diagnostic) => {
    start = Math.max(0, Math.min(points.length, start));
    end = Math.max(start, Math.min(points.length, end));
    intervals.push({ start, end, className, diagnostic, order: intervals.length });
  };
  for (const span of lexical) add(span.start, span.end, `fm-token-${span.kind}`);
  for (const [start, end] of changes) add(start, end, "fm-inline-change");
  if (query) {
    const lower = text.toLocaleLowerCase();
    const offsets = new Map([[0, 0]]);
    let unit = 0;
    points.forEach((char, index) => { unit += char.length; offsets.set(unit, index + 1); });
    for (let offset = 0; offset < text.length;) {
      const found = lower.indexOf(query, offset);
      if (found < 0) break;
      add(offsets.get(found) ?? points.length, offsets.get(found + query.length) ?? points.length, "fm-local-match");
      offset = found + query.length;
    }
  }
  const range = ({ start, end }) => [start.line === line ? start.character : 0, end.line === line ? end.character : points.length];
  const indexed = annotations(file, configuration, line);
  for (const token of indexed.tokens ?? []) {
    if (Object.hasOwn(colors, token.kind)) add(...range(token.range), `fm-token-${colors[token.kind]}`);
    if (token.modifiers.includes("deprecated")) add(...range(token.range), "fm-deprecated");
  }
  for (const diagnostic of indexed.diagnostics ?? []) {
    const tone = diagnostic.severity === "error" ? "fm-diagnostic"
      : diagnostic.severity === "warning" ? "fm-diagnostic-warning" : "fm-diagnostic-info";
    add(...range(diagnostic.range), tone, diagnostic);
  }
  const attach = (node, active) => {
    const syntax = active.filter((item) => item.className.startsWith("fm-token-")).reduce(
      (last, item) => !last || item.order > last.order ? item : last, undefined,
    );
    node.className = [...active.filter((item) => !item.className.startsWith("fm-token-")).map((item) => item.className), syntax?.className].filter(Boolean).join(" ");
    const items = [...new Set(active.map((item) => item.diagnostic).filter(Boolean))];
    if (items.length) {
      node.dataset.diagnostic = "";
      node.tabIndex = 0;
      node.setAttribute("aria-label", items.map((item) => item.message).join("\n"));
      diagnosticNodes.set(node, { items, configuration });
    }
  };
  const starts = new Map();
  const ends = new Map();
  for (const item of intervals) {
    if (!starts.has(item.start)) starts.set(item.start, []);
    if (!ends.has(item.end)) ends.set(item.end, []);
    starts.get(item.start).push(item);
    ends.get(item.end).push(item);
  }
  const boundaries = [...new Set([0, points.length, ...starts.keys(), ...ends.keys()])].sort((a, b) => a - b);
  const covering = new Set();
  boundaries.forEach((start, index) => {
    for (const item of ends.get(start) ?? []) covering.delete(item);
    const markers = [];
    for (const item of starts.get(start) ?? []) {
      if (item.end === item.start) markers.push(item);
      else covering.add(item);
    }
    if (markers.length) {
      const marker = doc.createElement("span");
      attach(marker, markers);
      marker.classList.add(markers.some((item) => item.diagnostic) ? "fm-diagnostic-zero" : "fm-inline-zero");
      target.append(marker);
    }
    const end = boundaries[index + 1];
    if (end === undefined) return;
    const active = [...covering];
    if (!active.length) { target.append(doc.createTextNode(points.slice(start, end).join(""))); return; }
    const span = doc.createElement("span");
    attach(span, active);
    span.textContent = points.slice(start, end).join("");
    target.append(span);
  });
}
