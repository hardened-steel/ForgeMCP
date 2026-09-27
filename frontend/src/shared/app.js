import { App, PostMessageTransport, applyDocumentTheme, applyHostFonts, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";
import { createResultView } from "./result-view.js";
import { loadResultDiff } from "./result-resources.js";
import "./widget.css";

/** Lifecycle bridge and immutable result-resource loading; never calls tools. */
export async function connectWidget({ toolName, describe, renderValue }) {
  const root = document.getElementById("widget");
  const view = createResultView(root, { toolName, describe, renderValue });
  const app = new App({ name: `ForgeMCP ${toolName}`, version: "0.2.0" });
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
    if (!data?.extensions_uri || result.isError || !("lines_added" in data || "replacements" in data)) return;
    try {
      const diff = await loadResultDiff(result, (params) => app.readServerResource(params), () => current === generation);
      if (current === generation) view.setExtensions({ diff, diffState: "ready" });
    } catch {
      if (current === generation) view.setExtensions({ diffState: "error" });
    }
  });
  app.addEventListener("toolcancelled", invalidate(view.cancelled));
  app.addEventListener("hostcontextchanged", contextChanged);
  app.onteardown = async () => { generation++; view.dispose(); return {}; };
  try {
    await app.connect(new PostMessageTransport());
    contextChanged(app.getHostContext());
  } catch { view.unavailable(); }
}
