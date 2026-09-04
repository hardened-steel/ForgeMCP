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
const asset = join(repositoryRoot, "src", "forgemcp", "apps", "assets", "project-status.html");

test("project status asset is a fresh single-file read-only Ext Apps view", async () => {
  execFileSync(process.execPath, [join(sourceDirectory, "build.mjs"), "--check"], { cwd: sourceDirectory });
  const [source, helper, html, lockfile] = await Promise.all([readFile(join(sourceDirectory, "project-status-app.js"), "utf8"), readFile(join(commonDirectory, "mcp-app.js"), "utf8"), readFile(asset, "utf8"), readFile(join(sourceDirectory, "..", "package-lock.json"), "utf8")]);
  assert.ok(html.startsWith("<!doctype html>")); assert.match(html, /source-sha256:[0-9a-f]{64}/); assert.equal((html.match(/<script>/g) || []).length, 1); assert.equal((html.match(/<style>/g) || []).length, 1);
  const scriptStart = html.indexOf("<script>") + "<script>".length; const scriptEnd = html.lastIndexOf("</script>"); new vm.Script(html.slice(scriptStart, scriptEnd), { filename: "project-status-production-asset.js" });
  assert.match(lockfile, /"@modelcontextprotocol\/ext-apps"/); assert.match(helper, /new App\(/); assert.match(helper, /new PostMessageTransport\(/);
  for (const handler of ["ontoolinput", "ontoolresult", "onhostcontextchanged", "onteardown", "safeAreaInsets"]) assert.match(helper, new RegExp(handler));
  for (const forbidden of ["callServerTool", "tools/call", "resources/read", "fetch(", "XMLHttpRequest", "WebSocket", "innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "Function(", "requestDisplayMode", "ui/open-link"]) assert.equal(`${source}\n${helper}`.includes(forbidden), false, forbidden);
  assert.match(source, /MAX_COMPONENTS = 64/); assert.match(source, /validStatus/); assert.match(source, /validComponent/); assert.match(source, /textContent/); assert.match(source, /ArrowLeft/); assert.match(source, /mouseenter/); assert.match(source, /safeText/);
});

test("project status model handling is bounded and fails closed", async () => {
  const source = await readFile(join(sourceDirectory, "project-status-app.js"), "utf8");
  const withoutImport = source.replace(/import\s*\{\s*connectMcpApp\s*\}\s*from "\.\.\/common\/mcp-app\.js";\s*/u, "");
  const isolated = `${withoutImport.slice(0, withoutImport.indexOf("\nconnectMcpApp("))}\nglobalThis.validateProjectStatus = validStatus;`;
  const context = vm.createContext({ document: { getElementById: () => ({}) }, globalThis: {} });
  new vm.Script(isolated, { filename: "project-status-model.js" }).runInContext(context);
  const validate = context.globalThis.validateProjectStatus;
  const sample = {
    health: "healthy", activity: "idle", generated_at: "2026-09-04T12:34:56Z", capabilities: ["project.status"], warnings: ["safe_notice"],
    components: [{ id: "core", display_name: "ForgeMCP Core", state: "active", summary: "Cached state.", warnings: [], observed_at: "2026-09-04T12:34:56Z" }],
  };
  assert.equal(validate({ structuredContent: sample }).components.length, 1);
  assert.equal(validate({ structuredContent: { ...sample, components: [] } }).components.length, 0);
  assert.equal(validate({ structuredContent: { ...sample, components: Array.from({ length: 65 }, () => sample.components[0]) } }), null);
  assert.equal(validate({ structuredContent: { ...sample, health: "unknown" } }), null);
  assert.equal(validate({ structuredContent: { ...sample, components: [{ id: "core" }] } }).components.length, 0);
  assert.equal(validate({ content: [{ text: JSON.stringify(sample) }] }).components[0].state, "active");
  assert.equal(validate({ content: [{ text: "not json" }] }), null);
  assert.equal(validate({ content: [{ text: "x".repeat(100001) }] }), null);
});
