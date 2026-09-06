import { App, PostMessageTransport, applyDocumentTheme, applyHostFonts, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";
import "./toolsets.css";

const app = new App({ name: "ForgeMCP toolsets", version: "0.2.0" });
const status = (text) => { document.getElementById("status").textContent = text; };
const node = (tag, text) => {
  const element = document.createElement(tag);
  element.textContent = text;
  return element;
};
let selected;
let summaries = [];
let revision = 0;

const showDetails = (data) => {
  const container = document.getElementById("details");
  container.replaceChildren(node("h2", data.name));
  for (const tool of data.tools) {
    const article = node("article", "");
    article.append(node("h3", tool.name));
    const fields = node("dl", "");
    for (const [label, value] of [["Kind", tool.kind.replaceAll("_", " ")], ["Path", tool.path], ["Version", tool.version ?? "Unknown"]]) {
      fields.append(node("dt", label), node("dd", value));
    }
    article.append(fields);
    container.append(article);
  }
  if (!data.tools.length) container.append(node("p", "No tools found in this toolset."));
};

const choose = async (id) => {
  selected = id;
  const request = ++revision;
  renderList();
  status("Reading toolset…");
  try {
    const result = await app.callServerTool({ name: "toolset_get", arguments: { toolset_id: id } });
    if (request !== revision) return;
    if (result.isError || !result.structuredContent) throw new Error();
    showDetails(result.structuredContent);
    status(`${summaries.length} toolsets available.`);
  } catch {
    if (request === revision) status("Could not read this toolset.");
  }
};

const renderList = () => {
  const items = summaries.map((item) => {
    const button = node("button", "");
    button.type = "button";
    button.setAttribute("aria-pressed", String(item.id === selected));
    button.append(node("strong", item.name), node("small", item.tools.join(", ") || "No tools found"));
    button.addEventListener("click", () => choose(item.id));
    return button;
  });
  document.getElementById("toolsets").replaceChildren(...items);
};

const render = async (result) => {
  const data = result?.structuredContent;
  if (result?.isError) { status("Could not read toolsets."); return; }
  if (!data) return;
  // The Python SDK wraps non-object return types in { result: ... }.
  if (Array.isArray(data.result)) {
    summaries = data.result;
    renderList();
    if (summaries.length) await choose(summaries[0].id);
    else status("No toolsets available.");
  } else if (data.id && Array.isArray(data.tools)) {
    ++revision;
    selected = data.id;
    showDetails(data);
    status("Toolset details ready.");
    try {
      const listing = await app.callServerTool({ name: "toolsets_list", arguments: {} });
      if (Array.isArray(listing.structuredContent?.result)) {
        summaries = listing.structuredContent.result;
        renderList();
      }
    } catch { status("Toolset details ready; list unavailable."); }
  }
};

const applyHostContext = (context) => {
  if (!context) return;
  if (context.theme) applyDocumentTheme(context.theme);
  if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
  if (context.styles?.css?.fonts) applyHostFonts(context.styles.css.fonts);
  if (context.safeAreaInsets) {
    const { top, right, bottom, left } = context.safeAreaInsets;
    document.body.style.padding = `${top}px ${right}px ${bottom}px ${left}px`;
  }
};
app.addEventListener("toolinput", () => status("Reading cached toolsets…"));
app.addEventListener("toolresult", render);
app.addEventListener("hostcontextchanged", applyHostContext);
app.onteardown = async () => { ++revision; return {}; };
await app.connect(new PostMessageTransport());
applyHostContext(app.getHostContext());
