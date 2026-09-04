import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = join(sourceDirectory, "..", "..");
const assetDirectory = join(repositoryRoot, "src", "forgemcp", "apps", "assets");
const commonDirectory = join(sourceDirectory, "..", "common");
const forbidden = ["callServerTool", "tools/call", "resources/read", "fetch(", "XMLHttpRequest", "WebSocket", "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "localStorage", "sessionStorage", "eval(", "Function(", "requestDisplayMode", "ui/open-link", "window.parent.postMessage"];

function validator(source, name) {
  const withoutImport = source.replace(/import\s*\{\s*connectMcpApp\s*\}\s*from "\.\.\/common\/mcp-app\.js";\s*/u, "");
  const isolated = `${withoutImport.slice(0, withoutImport.indexOf("\nconnectMcpApp("))}\nglobalThis.${name} = validView;`;
  const context = vm.createContext({ document: { getElementById: () => ({}) }, globalThis: {} });
  new vm.Script(isolated, { filename: `${name}.js` }).runInContext(context);
  return context.globalThis[name];
}

test("Quality App assets are fresh self-contained safe views", async () => {
  execFileSync(process.execPath, [join(sourceDirectory, "build.mjs"), "--check"], { cwd: sourceDirectory });
  const [overviewSource, findingsSource, helper, overview, findings] = await Promise.all([
    readFile(join(sourceDirectory, "quality-overview-app.js"), "utf8"),
    readFile(join(sourceDirectory, "quality-findings-app.js"), "utf8"),
    readFile(join(commonDirectory, "mcp-app.js"), "utf8"),
    readFile(join(assetDirectory, "quality-overview.html"), "utf8"),
    readFile(join(assetDirectory, "quality-findings.html"), "utf8"),
  ]);
  for (const [name, html, source] of [["overview", overview, overviewSource], ["findings", findings, findingsSource]]) {
    assert.ok(html.startsWith("<!doctype html>"), name); assert.match(html, /source-sha256:[0-9a-f]{64}/); assert.ok(html.length < 768 * 1024); assert.equal((html.match(/<script>/g) || []).length, 1); assert.equal((html.match(/<style>/g) || []).length, 1); new vm.Script(html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>")), { filename: `quality-${name}.html` }); assert.match(source, /textContent/); assert.match(source, /safeText/);
    for (const token of forbidden) assert.equal(`${source}\n${helper}`.includes(token), false, `${name}:${token}`);
  }
  assert.match(overview, /forgemcp-quality-overview/); assert.match(findings, /forgemcp-quality-findings/); assert.match(helper, /new App\(/); assert.match(helper, /new PostMessageTransport\(/);
});

test("Quality overview accepts bounded status, format, and check-list public results", async () => {
  const source = await readFile(join(sourceDirectory, "quality-overview-app.js"), "utf8"); const validate = validator(source, "validateQualityOverview");
  const status = { clang_format: { available: true, version: "22.1.8", executable: "clang-format" }, clang_tidy: { available: false, error: "not qualified" }, sanitizer_parsers: ["address_sanitizer", "unknown"] };
  assert.equal(validate({ structuredContent: status }).kind, "status");
  assert.equal(validate({ content: [{ text: JSON.stringify({ files: [{ path: "src/main.cpp", would_change: true }], clean: false }) }] }).files.length, 1);
  assert.equal(validate({ structuredContent: { checks: ["modernize-use-nullptr"], truncated: false } }).checks[0], "modernize-use-nullptr");
  assert.equal(validate({ structuredContent: { checks: Array.from({ length: 2049 }, () => "x"), truncated: false } }), null);
  assert.equal(validate({ content: [{ text: "not json" }] }), null);
});

test("Quality findings accepts bounded normalized public results and rejects malformed input", async () => {
  const source = await readFile(join(sourceDirectory, "quality-findings-app.js"), "utf8"); const validate = validator(source, "validateQualityFindings");
  const diagnostic = { message: "use nullptr", severity: "warning", location: { uri: "file:///workspace/src/main.cpp", range: { start: { line: 4, column: 2 } } }, code: "modernize-use-nullptr", source: "clang-tidy" };
  assert.equal(validate({ structuredContent: { diagnostics: [diagnostic], omitted_external_count: 1, omitted_invalid_count: 0, truncated: false, complete: true, execution_state: "completed" } }).diagnostics.length, 1);
  const finding = { kind: "address_sanitizer", category: "heap-use-after-free", summary: "AddressSanitizer reported heap-use-after-free.", frames: [{ function: "worker", address: "0x123", location: null }], omitted_external_count: 0, truncated: false, complete: true };
  assert.equal(validate({ content: [{ text: JSON.stringify({ findings: [finding], truncated: false, complete: true, omitted_external_count: 0 }) }] }).findings.length, 1);
  assert.equal(validate({ structuredContent: { diagnostics: [diagnostic], truncated: false, complete: true, execution_state: "unknown" } }), null);
  assert.equal(validate({ structuredContent: { findings: Array.from({ length: 33 }, () => finding), truncated: false, complete: true } }), null);
});
