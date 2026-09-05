import {
  App,
  PostMessageTransport,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
} from "@modelcontextprotocol/ext-apps";

import "./workspace-overview.css";

const app = new App({ name: "ForgeMCP workspace overview", version: "0.2.0" });

const text = (id, value) => {
  document.getElementById(id).textContent = String(value);
};

const toggle = (id, enabled) => {
  document.getElementById(id).classList.toggle("enabled", Boolean(enabled));
};

const render = (result) => {
  const data = result?.structuredContent;
  if (!data) return;
  text("workspace-name", data.name);
  text("source-count", data.source_files);
  text("header-count", data.header_files);
  toggle("cmake-list", data.has_cmake_lists);
  toggle("cmake-presets", data.has_cmake_presets);
  text("status", data.scan_truncated ? "Scan limit reached; counts are partial." : "Scan complete.");
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

app.addEventListener("toolinput", () => text("status", "Scanning workspace…"));
app.addEventListener("toolresult", render);
app.addEventListener("hostcontextchanged", applyHostContext);
app.onteardown = async () => ({});

await app.connect(new PostMessageTransport());
applyHostContext(app.getHostContext());
