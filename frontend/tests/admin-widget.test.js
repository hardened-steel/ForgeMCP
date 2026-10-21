import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { JSDOM } from "jsdom";
import { App, McpUiInitializeResultSchema } from "@modelcontextprotocol/ext-apps";
import { ToolSchema, CallToolResultSchema } from "@modelcontextprotocol/sdk/types.js";
import { mountInvocationWidget } from "../src/admin-widget.js";

// Python tests compare InstanceAgent.send's exact wire output with this same fixture.
const wire = JSON.parse(await readFile(new URL("../../tests/admin/fixtures/widget-wire.json", import.meta.url), "utf8"));
const sandboxOrigin = "http://127.0.0.1:18766";
const tick = () => new Promise((resolve) => setImmediate(resolve));

/** Run the production browser host against a real SDK App, without loading UI scripts. */
async function host(t, call = structuredClone(wire.result.history[0])) {
  const dom = new JSDOM("<main></main>", { url: "http://127.0.0.1:18765/" });
  const win = dom.window;
  const globals = {
    window: win, document: win.document, location: win.location, navigator: win.navigator,
    ResizeObserver: class {
      observe() {}
      disconnect() {}
    },
  };
  const restoreGlobals = [];
  for (const [name, value] of Object.entries(globals)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { value, configurable: true });
    restoreGlobals.push(() => {
      if (previous) Object.defineProperty(globalThis, name, previous);
      else delete globalThis[name];
    });
  }
  t.mock.method(console, "debug", () => {});
  const createElement = win.document.createElement.bind(win.document);
  t.mock.method(win.document, "createElement", (tag, ...options) => {
    const node = createElement(tag, ...options);
    if (tag === "iframe") {
      // Model navigation without fetching a server or replacing jsdom's Window identity.
      let source = "";
      Object.defineProperty(node, "src", { get: () => source, set: (value) => { source = value; } });
    }
    return node;
  });
  const tool = structuredClone(wire.result.tools[0]);
  const uri = tool._meta.ui.resourceUri;
  const reads = [];
  const html = "<!doctype html><main id='widget'></main>";
  const readResource = async (requested) => {
    reads.push(requested);
    return { contents: [{ uri: requested, mimeType: requested === uri ? "text/html;profile=mcp-app" : "application/json", text: requested === uri ? html : '{"error":null}' }] };
  };
  const abort = new AbortController();
  let widget;
  const app = new App({ name: "Regression App", version: "1.0.0" }, {}, { autoResize: false });
  const inputs = [];
  const results = [];
  app.addEventListener("toolinput", (params) => inputs.push(params));
  app.addEventListener("toolresult", (result) => results.push(result));
  app.onteardown = async () => ({});
  t.after(async () => {
    try {
      if (widget) await widget.dispose();
      abort.abort();
      await mounted.catch(() => {});
      await app.close();
      await tick();
    } finally {
      dom.window.close();
      for (const restore of restoreGlobals.reverse()) restore();
    }
  });
  const mounted = mountInvocationWidget({
    container: win.document.querySelector("main"), tool, call, sandboxOrigin,
    readResource, theme: () => "dark", signal: abort.signal,
  });
  mounted.catch(() => {});
  await tick();
  const frame = win.document.querySelector("iframe");
  assert.ok(frame);
  assert.equal(new URL(frame.src).origin, sandboxOrigin);
  const peer = frame.contentWindow;
  const outgoing = [];
  const transport = {
    async start() {},
    async close() { this.onclose?.(); },
    async send(message) {
      queueMicrotask(() => win.dispatchEvent(new win.MessageEvent("message", {
        data: structuredClone(message), source: peer, origin: sandboxOrigin,
      })));
    },
  };
  peer.postMessage = (message) => {
    outgoing.push(structuredClone(message));
    if (message.method !== "ui/notifications/sandbox-resource-ready") {
      queueMicrotask(() => transport.onmessage?.(structuredClone(message)));
    }
  };
  const ready = async () => {
    win.dispatchEvent(new win.MessageEvent("message", {
      data: { jsonrpc: "2.0", method: "ui/notifications/sandbox-proxy-ready", params: {} },
      source: peer, origin: sandboxOrigin,
    }));
    await tick();
    assert.equal(outgoing[0].params.html, html);
    await app.connect(transport, { timeout: 1000 });
    widget = await mounted;
    await tick();
  };
  return { win, app, ready, mounted, abort, reads, inputs, results, call, tool };
}

test("the Python IPC snapshot satisfies the Apps tool and result schemas without losing domain nulls", () => {
  const tool = wire.result.tools[0];
  const call = wire.result.history[0];
  assert.equal(ToolSchema.safeParse(tool).success, true);
  assert.equal(CallToolResultSchema.safeParse(call.result).success, true);
  assert.equal(tool.inputSchema.properties.path.default, null);
  assert.equal(call.arguments.end_line, null);
  assert.equal(call.result.structuredContent.error, null);
  assert.deepEqual(call.result.structuredContent.details.values, [null, "α"]);
  assert.ok(call.id && call.started_at);
  assert.deepEqual(call.progress, []);
});

test("nullable optional tool metadata reproduces the rejected initialize result", () => {
  const result = {
    protocolVersion: "2026-01-26", hostInfo: { name: "admin", version: "1.0.0" },
    hostCapabilities: { serverResources: {} },
    hostContext: { toolInfo: { id: wire.result.history[0].id, tool: wire.result.tools[0] } },
  };
  assert.equal(McpUiInitializeResultSchema.safeParse(result).success, true);
  const invalid = structuredClone(result);
  invalid.hostContext.toolInfo.tool.title = null;
  invalid.hostContext.toolInfo.tool.icons[0].theme = null;
  const rejected = McpUiInitializeResultSchema.safeParse(invalid);
  assert.equal(rejected.success, false);
  assert.ok(rejected.error.issues.some((issue) => issue.path.join(".") === "hostContext.toolInfo.tool.title"));
});

test("the admin host completes the real SDK handshake and supplies the original invocation", { timeout: 3000 }, async (t) => {
  const fixture = await host(t);
  await fixture.ready();
  assert.equal(fixture.app.getHostContext().toolInfo.tool.name, fixture.tool.name);
  assert.equal(fixture.app.getHostContext().theme, "dark");
  assert.deepEqual(fixture.inputs, [{ arguments: fixture.call.arguments }]);
  assert.deepEqual(fixture.results, [fixture.call.result]);
  assert.deepEqual(fixture.reads, [fixture.tool._meta.ui.resourceUri]);
});

test("a connected result App can read its linked resource but cannot read other resources or execute tools", { timeout: 3000 }, async (t) => {
  const call = structuredClone(wire.result.history[0]);
  const linked = "forgemcp://workspace/results/0123456789abcdef0123456789abcdef/diff.json";
  call.result.structuredContent.resources = { diff: { uri: linked, mime_type: "application/json" } };
  const fixture = await host(t, call);
  await fixture.ready();
  const response = await fixture.app.readServerResource({ uri: linked });
  assert.equal(response.contents[0].uri, linked);
  assert.equal(response.contents[0].text, '{"error":null}');
  await assert.rejects(fixture.app.readServerResource({ uri: "forgemcp://workspace/file?path=project/secret" }), /Only resources linked/);
  await assert.rejects(fixture.app.callServerTool({ name: "workspace_delete", arguments: { path: "project/main.cpp" } }));
  assert.deepEqual(fixture.reads, [fixture.tool._meta.ui.resourceUri, linked]);
});

test("cancelling a pending handshake closes the host and removes its frame", { timeout: 3000 }, async (t) => {
  const fixture = await host(t);
  fixture.abort.abort();
  await assert.rejects(fixture.mounted, { name: "AbortError" });
  assert.equal(fixture.win.document.querySelector("iframe"), null);
});
