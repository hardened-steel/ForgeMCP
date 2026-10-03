import { App, PostMessageTransport, applyDocumentTheme, applyHostFonts, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";
import { createResultView } from "./result-view.js";
import { loadResultClangd, loadResultDiff } from "./result-resources.js";
import "./widget.css";

/** Lifecycle bridge and immutable result-resource loading; never calls tools. */
export async function connectWidget({ toolName, describe, renderValue }) {
  const root = document.getElementById("widget");
  const app = new App({ name: `ForgeMCP ${toolName}`, version: "0.2.0" });
  const view = createResultView(root, {
    toolName, renderValue,
    describe: (data, resources) => describe(data, resources, app.getHostContext()?.toolInfo?.tool?.name ?? toolName),
  });
  let generation = 0;
  const invalidate = (handler) => () => { generation++; handler(); };
  const contextChanged = (context) => {
    if (!context) return;
    if (context.theme) applyDocumentTheme(context.theme);
    if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
    if (context.styles?.css?.fonts) applyHostFonts(context.styles.css.fonts);
    if (context.safeAreaInsets) {
      for (const side of ["top", "right", "bottom", "left"]) {
        const value = context.safeAreaInsets[side] ?? 0;
        root.style.setProperty(`--fm-safe-${side}`, `${Math.max(0, value)}px`);
      }
    }
  };
  app.addEventListener("toolinput", invalidate(view.pending));
  app.addEventListener("toolinputpartial", invalidate(view.pending));
  app.addEventListener("toolresult", async (result) => {
    const current = ++generation;
    view.receive(result);
    const data = result?.structuredContent;
    if (result?.isError || data?.action === "moved") return;
    await Promise.all([
      ["diff", loadResultDiff], ["clangd", loadResultClangd],
    ].filter(([name]) => data?.resources?.[name]).map(async ([name, load]) => {
      try {
        const resource = await load(result, (params) => app.readServerResource(params), () => current === generation);
        if (current === generation) view.setResources({ [name]: resource, [`${name}State`]: "ready" });
      } catch {
        if (current === generation) view.setResources({ [`${name}State`]: "error" });
      }
    }));
  });
  app.addEventListener("toolcancelled", invalidate(view.cancelled));
  app.addEventListener("hostcontextchanged", contextChanged);
  app.onteardown = async () => { generation++; view.dispose(); return {}; };
  try {
    await app.connect(new PostMessageTransport());
    contextChanged(app.getHostContext());
  } catch { view.unavailable(); }
}
