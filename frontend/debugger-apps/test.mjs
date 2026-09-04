import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const root = join(sourceDirectory, "..", "..");
const assets = ["debugger-session.html", "debugger-stack.html", "debugger-data.html"];
const sources = ["debugger-session-app.js", "debugger-stack-app.js", "debugger-data-app.js"];
test("debugger Apps are fresh, static, and safely bounded", async () => {
  execFileSync(process.execPath, [join(sourceDirectory, "build.mjs"), "--check"], { cwd: sourceDirectory });
  const helper = await readFile(join(sourceDirectory, "..", "common", "mcp-app.js"), "utf8");
  for (const [index, sourceName] of sources.entries()) {
    const [source, html] = await Promise.all([readFile(join(sourceDirectory, sourceName), "utf8"), readFile(join(root, "src", "forgemcp", "apps", "assets", assets[index]), "utf8")]);
    assert.match(html, /^<!doctype html><!-- source-sha256:[0-9a-f]{64} -->/);
    const start = html.indexOf("<script>") + 8; const end = html.lastIndexOf("</script>"); new vm.Script(html.slice(start, end), { filename: assets[index] });
    for (const forbidden of ["callServerTool", "tools/call", "resources/read", "fetch(", "XMLHttpRequest", "WebSocket", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function(", "localStorage", "ui/open-link"]) assert.equal(`${source}\n${helper}`.includes(forbidden), false, forbidden);
    assert.match(source, /textContent/); assert.match(source, /safeText/); assert.match(source, /structuredContent/);
  }
});
test("debugger result classifiers accept public results and reject oversized data", async () => {
  async function classifier(sourceName, functionName) {
    const source = await readFile(join(sourceDirectory, sourceName), "utf8");
    const cleaned = source.replace(/import\s*\{\s*connectMcpApp\s*\}\s*from "\.\.\/common\/mcp-app\.js";\s*/u, "");
    const body = cleaned.slice(0, cleaned.indexOf("function render()"));
    const context = vm.createContext({ document: { getElementById: () => ({}) }, globalThis: {} });
    new vm.Script(`${body}\nglobalThis.validate = ${functionName};`).runInContext(context);
    return context.globalThis.validate;
  }
  const session = await classifier("debugger-session-app.js", "validView");
  const stack = await classifier("debugger-stack-app.js", "validStack");
  const data = await classifier("debugger-data-app.js", "validData");
  assert.equal(session({ structuredContent: { state: "paused", backend_id: "lldb-dap", session_generation: 1, stop_generation: 2 } }).kind, "status");
  assert.equal(session({ structuredContent: { events: Array.from({ length: 257 }, () => ({})), next_cursor: 1 } }), null);
  assert.equal(stack({ structuredContent: { frames: [{ index: 0, name: "main", source: { kind: "workspace", path: "src/main.cpp" }, line: 3, column: 1 }] } }).items[0].path, "src/main.cpp");
  assert.equal(stack({ structuredContent: { threads: Array.from({ length: 257 }, () => ({})) } }), null);
  assert.equal(data({ structuredContent: { variables: [{ name: "value", value: "42", type: "int", variables_id: "opaque", named_variables: 0, indexed_variables: 0, truncated: false }] } }).items[0].value, "42");
  assert.equal(data({ structuredContent: { variables: Array.from({ length: 201 }, () => ({})) } }), null);
  assert.equal(data({ content: [{ text: JSON.stringify({ result: "42", type: "int", side_effects_possible: true, truncated: false }) }] }).kind, "evaluate");
});
