import { App, PostMessageTransport, applyDocumentTheme, applyHostFonts, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";
import { timestamp } from "./shared/presentation.js";
import "./shared/widget.css";
import "./process.css";

const root = document.getElementById("widget");
root.classList.add("fm-widget");
const make = (tag, className = "", value = "") => {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = value;
  return node;
};
const frame = make("div", "fm-window");
const title = make("header", "fm-titlebar", "[ ForgeMCP ]  process_get");
const meta = make("div", "fm-process-meta");
const info = make("details", "fm-process-info");
info.append(make("summary", "", "Process settings"));
const tabs = make("div", "fm-process-tabs");
tabs.setAttribute("role", "tablist");
const scroll = make("div", "fm-scroll");
const footer = make("footer", "fm-footer", "Waiting for process result…");
footer.setAttribute("role", "status");
frame.append(title, meta, info, tabs, scroll, footer);
root.append(frame);

const baseColors = ["#242424", "#bb4048", "#43804a", "#9a771f", "#456cb2", "#9255a0", "#2b8b91", "#b4b4b4"];
const brightColors = ["#666666", "#ed6a70", "#73b77a", "#d2ad4d", "#7b9ee8", "#c68bd4", "#6bcbd0", "#f2f2f2"];
function color(code) { return code < 8 ? baseColors[code] : brightColors[code - 8]; }
function applySgr(state, raw) {
  const codes = raw === "" ? [0] : raw.split(";").map(Number);
  for (let i = 0; i < codes.length; i++) {
    const code = codes[i];
    if (code === 0) { state.fg = ""; state.bg = ""; state.bold = false; }
    else if (code === 1) state.bold = true;
    else if (code === 22) state.bold = false;
    else if (code === 39) state.fg = "";
    else if (code === 49) state.bg = "";
    else if (code >= 30 && code <= 37) state.fg = color(code - 30);
    else if (code >= 90 && code <= 97) state.fg = color(code - 90 + 8);
    else if (code >= 40 && code <= 47) state.bg = color(code - 40);
    else if (code >= 100 && code <= 107) state.bg = color(code - 100 + 8);
    else if ((code === 38 || code === 48) && codes[i + 1] === 2 && i + 4 < codes.length) {
      const rgb = codes.slice(i + 2, i + 5);
      if (rgb.every((part) => Number.isInteger(part) && part >= 0 && part <= 255)) {
        state[code === 38 ? "fg" : "bg"] = `rgb(${rgb.join(",")})`;
      }
      i += 4;
    } else if ((code === 38 || code === 48) && codes[i + 1] === 5 && i + 2 < codes.length) {
      const index = codes[i + 2];
      if (index >= 0 && index < 16) state[code === 38 ? "fg" : "bg"] = color(index);
      else if (index >= 16 && index < 232) {
        const n = index - 16;
        const levels = [0, 95, 135, 175, 215, 255];
        state[code === 38 ? "fg" : "bg"] = `rgb(${levels[Math.floor(n / 36)]},${levels[Math.floor(n / 6) % 6]},${levels[n % 6]})`;
      } else if (index >= 232 && index < 256) {
        const gray = 8 + (index - 232) * 10;
        state[code === 38 ? "fg" : "bg"] = `rgb(${gray},${gray},${gray})`;
      }
      i += 2;
    }
  }
}

function appendColored(target, text, state) {
  const input = state.pending + text;
  state.pending = "";
  let cursor = 0;
  const add = (value) => {
    if (!value) return;
    const segment = make("span", "", value);
    if (state.fg) segment.style.color = state.fg;
    if (state.bg) segment.style.backgroundColor = state.bg;
    if (state.bold) segment.style.fontWeight = "bold";
    target.append(segment);
  };
  while (cursor < input.length) {
    const start = input.indexOf("\x1b[", cursor);
    if (start < 0) { add(input.slice(cursor)); break; }
    add(input.slice(cursor, start));
    const end = input.slice(start + 2).search(/[A-Za-z]/);
    if (end < 0) { state.pending = input.slice(start); break; }
    const commandIndex = start + 2 + end;
    if (input[commandIndex] === "m") applySgr(state, input.slice(start + 2, commandIndex));
    cursor = commandIndex + 1;
  }
  // A lone ESC may be the first byte of a sequence in the next chunk.
  if (state.pending === "" && input.endsWith("\x1b")) {
    const last = target.lastChild;
    if (last?.textContent.endsWith("\x1b")) last.textContent = last.textContent.slice(0, -1);
    state.pending = "\x1b";
  }
}

let details;
let activeTab = "combined";
function render() {
  scroll.replaceChildren();
  tabs.replaceChildren();
  meta.textContent = "";
  info.replaceChildren(make("summary", "", "Process settings"));
  if (!details) return;
  const process = details.process;
  meta.textContent = `#${process.process_id} · ${process.executable} · PID ${process.pid} · ${process.outcome} · ${timestamp(process.started)?.readable ?? process.started}`;
  const settings = make("dl");
  for (const [key, value] of [
    ["Arguments", process.arguments.join(" ") || "None"],
    ["Working directory", process.cwd],
    ["Encoding", process.encoding],
    ["Work time", `${process.work_time.toFixed(2)} s`],
    ["Timeout", JSON.stringify(process.timeout)],
  ]) {
    settings.append(make("dt", "", key), make("dd", "", String(value)));
  }
  info.append(settings);
  const streams = ["combined", "stdin", "stdout", "stderr"];
  for (const stream of streams) {
    const tab = make("button", "", stream === "combined" ? "Combined" : stream);
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", String(stream === activeTab));
    tab.addEventListener("click", () => { activeTab = stream; render(); });
    tabs.append(tab);
  }
  const transcript = activeTab === "combined" ? details.transcript : details.transcript.filter((entry) => entry.stream === activeTab);
  const log = make("pre", "fm-process-log");
  const states = new Map();
  transcript.forEach((entry, index) => {
    const state = states.get(entry.stream) ?? { fg: "", bg: "", bold: false, pending: "" };
    states.set(entry.stream, state);
    const chunk = make("span", "fm-process-chunk");
    chunk.tabIndex = 0;
    const readable = timestamp(entry.timestamp)?.readable ?? entry.timestamp;
    const hint = `${entry.stream} · ${readable}`;
    chunk.setAttribute("aria-label", hint);
    chunk.addEventListener("mouseenter", () => { footer.textContent = hint; });
    chunk.addEventListener("focus", () => { footer.textContent = hint; });
    chunk.addEventListener("mouseleave", () => { footer.textContent = `${transcript.length} chunks`; });
    chunk.addEventListener("blur", () => { footer.textContent = `${transcript.length} chunks`; });
    appendColored(chunk, entry.text, state);
    log.append(chunk);
    if (activeTab === "combined" && !entry.text.endsWith("\n")) {
      const marker = make("span", "fm-process-break");
      marker.setAttribute("aria-label", "Chunk ended without a newline");
      log.append(marker);
      if (index < transcript.length - 1) log.append("\n");
    }
  });
  if (!transcript.length) log.textContent = "No stream chunks recorded.";
  scroll.append(log);
  footer.textContent = `${transcript.length} chunks · ${activeTab}`;
}

const app = new App({ name: "ForgeMCP process_get", version: "0.2.0" });
const contextChanged = (context) => {
  if (!context) return;
  if (context.theme) applyDocumentTheme(context.theme);
  if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
  if (context.styles?.css?.fonts) applyHostFonts(context.styles.css.fonts);
  if (context.safeAreaInsets) {
    for (const side of ["top", "right", "bottom", "left"]) {
      root.style.setProperty(`--fm-safe-${side}`, `${Math.max(0, context.safeAreaInsets[side] ?? 0)}px`);
    }
  }
};
app.addEventListener("toolinput", () => { details = undefined; render(); footer.textContent = "Reading process…"; });
app.addEventListener("toolresult", (result) => {
  if (result?.isError || !result?.structuredContent) {
    footer.textContent = "Process result unavailable.";
    return;
  }
  details = result.structuredContent;
  activeTab = "combined";
  render();
});
app.addEventListener("toolcancelled", () => { footer.textContent = "Tool invocation cancelled."; });
app.addEventListener("hostcontextchanged", contextChanged);
app.onteardown = async () => { details = undefined; return {}; };
try {
  await app.connect(new PostMessageTransport());
  contextChanged(app.getHostContext());
} catch { footer.textContent = "Unable to connect to the host."; }
