import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";

const directory = dirname(fileURLToPath(import.meta.url));
const root = join(directory, "..", "..");
const assets = ["clangd-session.html", "clangd-insight.html", "clangd-navigation.html", "clangd-change-hierarchy.html"];
const sources = ["shared.js", "clangd-session-app.js", "clangd-insight-app.js", "clangd-navigation-app.js", "clangd-change-hierarchy-app.js"];
test("clangd Apps are fresh, static Ext Apps views with no authored host calls", async () => {
  execFileSync(process.execPath, [join(directory, "build.mjs"), "--check"], { cwd: directory });
  const authored = (await Promise.all(sources.map((name) => readFile(join(directory, name), "utf8")))).join("\n");
  for (const forbidden of ["callServerTool", "tools/call", "resources/read", "fetch(", "XMLHttpRequest", "WebSocket", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function(", "localStorage", "ui/open-link", "ui/update-model-context"]) assert.equal(authored.includes(forbidden), false, forbidden);
  assert.ok(authored.includes("textContent")); assert.ok(authored.includes("structuredContent")); assert.ok(authored.includes("JSON.parse"));
  for (const assetName of assets) { const html = await readFile(join(root, "src", "forgemcp", "apps", "assets", assetName), "utf8"); assert.ok(html.startsWith("<!doctype html>")); assert.match(html, /source-sha256:[0-9a-f]{64}/); assert.equal((html.match(/<script>/g) || []).length, 1); const start = html.indexOf("<script>") + 8; new vm.Script(html.slice(start, html.lastIndexOf("</script>")), { filename: assetName }); }
});

async function validator(sourceName, functionName) {
  const [shared, source] = await Promise.all([readFile(join(directory, "shared.js"), "utf8"), readFile(join(directory, sourceName), "utf8")]);
  const app = source.replace(/import\s*\{\s*connectMcpApp\s*\}\s*from "\.\.\/common\/mcp-app\.js";\s*/u, "");
  const isolated = `${shared}\n${app.slice(0, app.indexOf("connectMcpApp("))}\nglobalThis.resultValidator=${functionName};`;
  const context = vm.createContext({ document: { getElementById: () => ({}) } });
  new vm.Script(isolated, { filename: sourceName }).runInContext(context);
  return context.resultValidator;
}

test("clangd App validators accept bounded public structured and text-fallback result shapes", async () => {
  const session = await validator("clangd-session-app.js", "validSession");
  assert.equal(session({ structuredContent: { available: true, state: "running", version: "18" } }).state, "running");
  assert.equal(session({ content: [{ text: JSON.stringify({ available: false, state: "stopped" }) }] }).state, "stopped");
  assert.equal(session({ structuredContent: { available: true, state: "other" } }), null);
  const insight = await validator("clangd-insight-app.js", "validInsight");
  assert.equal(insight({ structuredContent: { path: "a.cpp", diagnostics: [{ message: "safe", severity: "error" }] } }).type, "diagnostics");
  assert.equal(insight({ structuredContent: { path: "a.cpp", contents: "int value" } }).type, "hover");
  const navigation = await validator("clangd-navigation-app.js", "validNavigation");
  assert.equal(navigation({ structuredContent: { path: "a.cpp", locations: [{ path: "b.hpp", range: {} }], omitted_external_results: 0 } }).type, "locations");
  assert.equal(navigation({ structuredContent: { symbols: [{ name: "Thing", kind: "class", location: { path: "a.hpp", range: {} } }] } }).type, "workspace symbols");
  const change = await validator("clangd-change-hierarchy-app.js", "validChange");
  assert.equal(change({ structuredContent: { edit: { applied: true, no_op: false, changes: [], affected_files: 0 } } }).type, "workspace edit");
  assert.equal(change({ structuredContent: { actions: [{ title: "Fix", action_id: "opaque" }] } }).type, "code actions");
  assert.equal(change({ structuredContent: { actions: Array.from({ length: 101 }, () => ({})) } }), null);
});
