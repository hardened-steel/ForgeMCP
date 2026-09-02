import {
  App,
  PostMessageTransport,
  applyDocumentTheme,
  applyHostFonts,
  applyHostStyleVariables,
} from "@modelcontextprotocol/ext-apps";

export function applyHostPresentation(context = {}) {
  if (context.theme) applyDocumentTheme(context.theme);
  if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
  if (context.styles?.css?.fonts) applyHostFonts(context.styles.css.fonts);
  const insets = context.safeAreaInsets || {};
  for (const edge of ["top", "right", "bottom", "left"]) {
    document.documentElement.style.setProperty(`--forge-safe-${edge}`, `${Number(insets[edge]) || 0}px`);
  }
}

export function connectMcpApp(handlers) {
  const app = new App({ name: "forgemcp-git-status", version: "1.0.0" }, {}, { autoResize: false });
  app.ontoolinput = handlers.ontoolinput;
  app.ontoolresult = handlers.ontoolresult;
  app.onhostcontextchanged = (context) => {
    applyHostPresentation(context);
    handlers.onhostcontextchanged?.(context);
  };
  app.onteardown = async () => {
    handlers.onteardown?.();
    return {};
  };
  return app.connect(new PostMessageTransport(window.parent, window.parent)).then(() => {
    applyHostPresentation(app.getHostContext());
    return app;
  });
}
