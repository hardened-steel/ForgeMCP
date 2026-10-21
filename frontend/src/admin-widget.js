import { AppBridge, PostMessageTransport, getToolUiResourceUri } from "@modelcontextprotocol/ext-apps/app-bridge";

/** Identify only immutable result resources linked by the original invocation. */
function linkedUris(result) {
  const uris = new Set();
  const resources = result?.structuredContent?.resources;
  if (!resources || typeof resources !== "object") return uris;
  for (const resource of Object.values(resources)) {
    if (typeof resource?.uri === "string"
      && /^forgemcp:\/\/workspace\/results\/[0-9a-f]{32}\/[a-zA-Z0-9_-]+\.(json|md)$/.test(resource.uri)) {
      uris.add(resource.uri);
    }
  }
  return uris;
}

/** Mount one original tool invocation; the frame receives no execution capability. */
export async function mountInvocationWidget({ container, tool, call, sandboxOrigin, readResource, theme, signal }) {
  const uri = getToolUiResourceUri(tool);
  if (!uri) throw new Error("This tool has no widget resource.");
  const sandbox = new URL(sandboxOrigin);
  if (sandbox.origin === location.origin || sandbox.protocol !== "http:"
    || !["127.0.0.1", "localhost", "[::1]"].includes(sandbox.hostname)) {
    throw new Error("The widget sandbox requires a separate local origin.");
  }
  const resource = await readResource(uri);
  if (signal?.aborted) throw new DOMException("Widget loading cancelled.", "AbortError");
  const html = resource?.contents?.find((item) => item.uri === uri && typeof item.text === "string");
  if (!html || !String(html.mimeType ?? "").startsWith("text/html")) {
    throw new Error("The widget HTML resource is unavailable.");
  }
  const allowed = linkedUris(call.result);
  const frame = document.createElement("iframe");
  frame.title = `${call.tool_name} result widget`;
  frame.className = "admin-widget-frame";
  frame.setAttribute("sandbox", "allow-scripts allow-same-origin");
  let disposed = false;
  let initialized = false;
  let timeout;
  let resize;
  const context = () => ({
    toolInfo: { id: call.id, tool },
    theme: theme(),
    displayMode: "inline",
    availableDisplayModes: ["inline"],
    locale: navigator.language,
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    platform: "web",
    containerDimensions: { width: Math.max(1, container.clientWidth), maxHeight: 420 },
    safeAreaInsets: { top: 0, right: 0, bottom: 0, left: 0 },
    styles: { variables: { "--font-mono": '"Cascadia Code", Consolas, monospace' } },
  });
  const bridge = new AppBridge(
    null,
    { name: "ForgeMCP admin", version: "1.0.0" },
    { serverResources: {} },
    { hostContext: context() },
  );
  bridge.onreadresource = async ({ uri: requested }) => {
    if (disposed || !allowed.has(requested)) throw new Error("Only resources linked by this invocation are available.");
    return readResource(requested);
  };
  bridge.oncalltool = async () => { throw new Error("Result widgets cannot execute tools."); };
  bridge.onlistresources = async () => ({ resources: [] });
  bridge.onlistresourcetemplates = async () => ({ resourceTemplates: [] });
  bridge.onlistprompts = async () => ({ prompts: [] });
  bridge.onopenlink = async () => { throw new Error("External links are unavailable in result widgets."); };
  bridge.onsizechange = ({ height }) => {
    if (!disposed && Number.isFinite(height) && height > 0) frame.style.height = `${Math.min(420, height)}px`;
  };
  let resolveReady;
  let rejectReady;
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  ready.catch(() => {});
  bridge.onsandboxready = async () => {
    try {
      if (!disposed) await bridge.sendSandboxResourceReady({
        html: html.text,
        sandbox: "allow-scripts",
        csp: { connectDomains: [], resourceDomains: [], frameDomains: [] },
      });
    } catch (error) { rejectReady(error); }
  };
  bridge.oninitialized = async () => {
    if (disposed) return;
    try {
      initialized = true;
      await bridge.sendToolInput({ arguments: call.arguments ?? {} });
      if (call.result) await bridge.sendToolResult(call.result);
      else if (["cancelled", "interrupted"].includes(call.status)) {
        await bridge.sendToolCancelled({ reason: call.error ?? call.status });
      }
      clearTimeout(timeout);
      resolveReady();
    } catch (error) { rejectReady(error); }
  };
  const dispose = async () => {
    if (disposed) return;
    disposed = true;
    clearTimeout(timeout);
    resize?.disconnect();
    if (initialized) {
      try { await bridge.teardownResource({}, { timeout: 1000 }); } catch { /* A closed frame may no longer answer. */ }
    }
    try { await bridge.close(); } catch { /* Closing a disconnected bridge is harmless. */ }
    frame.remove();
  };
  const aborted = () => {
    rejectReady(new DOMException("Widget loading cancelled.", "AbortError"));
    void dispose();
  };
  signal?.addEventListener("abort", aborted, { once: true });
  container.append(frame);
  try {
    await bridge.connect(new PostMessageTransport(frame.contentWindow, frame.contentWindow));
    if (disposed || signal?.aborted) throw new DOMException("Widget loading cancelled.", "AbortError");
    timeout = setTimeout(() => rejectReady(new Error("The widget did not complete its connection handshake.")), 12000);
    const url = new URL("/sandbox.html", sandbox);
    url.hash = new URLSearchParams({ parent_origin: location.origin }).toString();
    frame.src = url.href;
    await ready;
    resize = new ResizeObserver(() => { if (!disposed) bridge.setHostContext(context()); });
    resize.observe(container);
    return {
      dispose: async () => { signal?.removeEventListener("abort", aborted); await dispose(); },
      refreshContext: () => { if (!disposed) bridge.setHostContext(context()); },
    };
  } catch (error) {
    signal?.removeEventListener("abort", aborted);
    await dispose();
    throw error;
  }
}
