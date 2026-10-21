/** Relay one packaged App through a separate-origin, network-restricted sandbox. */
const parentOrigin = new URLSearchParams(location.hash.slice(1)).get("parent_origin");
let parentUrl;
try { parentUrl = new URL(parentOrigin); } catch { /* An invalid parent never receives messages. */ }
const loopback = (url) => url?.protocol === "http:"
  && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname);

if (window.parent !== window && loopback(parentUrl) && parentUrl.origin !== location.origin) {
  let view = null;
  const internal = (message) => typeof message?.method === "string"
    && message.method.startsWith("ui/notifications/sandbox-");
  window.addEventListener("message", (event) => {
    const message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (event.source === window.parent && event.origin === parentUrl.origin) {
      if (message.method === "ui/notifications/sandbox-resource-ready") {
        if (view || typeof message.params?.html !== "string") return;
        const parsed = new DOMParser().parseFromString(message.params.html, "text/html");
        const csp = parsed.createElement("meta");
        csp.httpEquiv = "Content-Security-Policy";
        csp.content = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
          + "img-src data: blob:; font-src data:; connect-src 'none'; frame-src 'none'; "
          + "object-src 'none'; base-uri 'none'; form-action 'none'; navigate-to 'none'";
        parsed.head.prepend(csp);
        view = document.createElement("iframe");
        view.title = "Tool result";
        view.setAttribute("sandbox", "allow-scripts");
        view.style.cssText = "display:block;width:100%;height:100%;border:0";
        view.srcdoc = `<!doctype html>${parsed.documentElement.outerHTML}`;
        document.body.replaceChildren(view);
      } else if (!internal(message) && view) {
        view.contentWindow.postMessage(message, "*");
      }
    } else if (view && event.source === view.contentWindow && !internal(message)) {
      window.parent.postMessage(message, parentUrl.origin);
    }
  });
  document.documentElement.style.cssText = "height:100%;margin:0;background:transparent";
  document.body.style.cssText = "height:100%;margin:0;background:transparent";
  window.parent.postMessage({
    jsonrpc: "2.0", method: "ui/notifications/sandbox-proxy-ready", params: {},
  }, parentUrl.origin);
}
