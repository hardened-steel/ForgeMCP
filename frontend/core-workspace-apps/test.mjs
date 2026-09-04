import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = join(sourceDirectory, "..", "..");
const commonDirectory = join(sourceDirectory, "..", "common");
const assetsDirectory = join(repositoryRoot, "src", "forgemcp", "apps", "assets");
const forbidden = ["callServerTool", "tools/call", "resources/read", "fetch(", "XMLHttpRequest", "WebSocket", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "Function(", "localStorage", "sessionStorage", "ui/open-link", "ui/update-model-context", "requestDisplayMode", "window.parent.postMessage"];

async function source(name) { return readFile(join(sourceDirectory, name), "utf8"); }
function validatorSource(value, name) {
  const withoutImport = value.replace(/import\s*\{\s*connectMcpApp\s*\}\s*from "\.\.\/common\/mcp-app\.js";\s*/u, "");
  return `${withoutImport.slice(0, withoutImport.indexOf("\nconnectMcpApp("))}\nglobalThis.${name} = ${name};`;
}

test("Core and Workspace assets are fresh, static Ext Apps views", async () => {
  execFileSync(process.execPath, [join(sourceDirectory, "build.mjs"), "--check"], { cwd: sourceDirectory });
  const [serverSource, workspaceSource, helper] = await Promise.all([source("server-status-app.js"), source("workspace-result-app.js"), readFile(join(commonDirectory, "mcp-app.js"), "utf8")]);
  for (const value of [serverSource, workspaceSource, helper]) for (const token of forbidden) assert.equal(value.includes(token), false, token);
  for (const name of ["server-status.html", "workspace-result.html"]) {
    const html = await readFile(join(assetsDirectory, name), "utf8");
    assert.ok(html.startsWith("<!doctype html>")); assert.match(html, /source-sha256:[0-9a-f]{64}/); assert.equal((html.match(/<script>/g) || []).length, 1); assert.equal((html.match(/<style>/g) || []).length, 1);
    const start = html.indexOf("<script>") + "<script>".length; new vm.Script(html.slice(start, html.lastIndexOf("</script>")), { filename: name });
  }
  assert.match(serverSource, /textContent/); assert.match(workspaceSource, /textContent/); assert.match(workspaceSource, /MAX_FILES = 1000/); assert.match(workspaceSource, /MAX_RENDER_TEXT/);
});

test("server status accepts the public structured and textual result forms", async () => {
  const context = vm.createContext({ document: { getElementById: () => ({}) } });
  new vm.Script(validatorSource(await source("server-status-app.js"), "validServerStatus"), { filename: "server-status-model.js" }).runInContext(context);
  const sample = { version: "0.1.0", workspace_root: "configured", state: "running", services: ["workspace"] };
  assert.equal(context.validServerStatus({ structuredContent: sample }).state, "running");
  assert.equal(context.validServerStatus({ content: [{ text: JSON.stringify(sample) }] }).services.length, 1);
  assert.equal(context.validServerStatus({ structuredContent: { ...sample, state: "healthy" } }), null);
  assert.equal(context.validServerStatus({ structuredContent: { ...sample, services: Array(65).fill("x") } }), null);
});

test("workspace result classification is bounded and fails closed", async () => {
  const context = vm.createContext({ document: { getElementById: () => ({}) } });
  new vm.Script(validatorSource(await source("workspace-result-app.js"), "validWorkspaceResult"), { filename: "workspace-result-model.js" }).runInContext(context);
  const snapshot = { exists: true, size_bytes: 4, sha256: "a".repeat(64), modified_at: null, captured_at: "2026-09-04T12:34:56Z" };
  const files = { files: [{ path: "src/main.cpp", snapshot }] };
  assert.equal(context.validWorkspaceResult({ structuredContent: files }).kind, "files");
  assert.equal(context.validWorkspaceResult({ content: [{ text: JSON.stringify({ path: "src/main.cpp", text: "int main() {}\n", snapshot }) }] }).kind, "text");
  assert.equal(context.validWorkspaceResult({ structuredContent: { applied: false, changes: [] } }).kind, "mutation");
  assert.equal(context.validWorkspaceResult({ structuredContent: { files: Array(1001).fill(files.files[0]) } }), null);
  assert.equal(context.validWorkspaceResult({ structuredContent: { path: "x", text: "a".repeat(262145), snapshot } }), null);
  assert.equal(context.validWorkspaceResult({ structuredContent: { path: "x", snapshot: { exists: "yes" } } }), null);
});
