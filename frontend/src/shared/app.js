import { App, PostMessageTransport, applyDocumentTheme, applyHostFonts, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";
import { createResultView } from "./result-view.js";
import "./widget.css";

/** Lifecycle bridge only; no tool calls, resource reads, polling, or result fetching. */
export async function connectWidget({ toolName, describe }) {
  const root = document.getElementById("widget");
  const view = createResultView(root, { toolName, describe });
  const app = new App({ name: `ForgeMCP ${toolName}`, version: "0.2.0" });
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
  app.addEventListener("toolinput", view.pending);
  app.addEventListener("toolinputpartial", view.pending);
  app.addEventListener("toolresult", view.receive);
  app.addEventListener("toolcancelled", view.cancelled);
  app.addEventListener("hostcontextchanged", contextChanged);
  app.onteardown = async () => { view.dispose(); return {}; };
  try {
    await app.connect(new PostMessageTransport());
    contextChanged(app.getHostContext());
  } catch { view.unavailable(); }
}
