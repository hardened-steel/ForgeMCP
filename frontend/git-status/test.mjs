import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";
import { runBrowserHarness } from "./browser-harness.mjs";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = join(sourceDirectory, "..", "..");
const commonDirectory = join(sourceDirectory, "..", "common");
const asset = join(repositoryRoot, "src", "forgemcp", "apps", "assets", "git-status.html");

test("production asset bundles official ext-apps and has no refresh or privileged UI action", async () => {
  execFileSync(process.execPath, [join(sourceDirectory, "build.mjs"), "--check"], { cwd: sourceDirectory });
  const [template, theme, css, javascript, helper, html, lockfile] = await Promise.all([
    readFile(join(sourceDirectory, "template.html"), "utf8"),
    readFile(join(commonDirectory, "theme.css"), "utf8"),
    readFile(join(sourceDirectory, "git-status-app.css"), "utf8"),
    readFile(join(sourceDirectory, "git-status-app.js"), "utf8"),
    readFile(join(commonDirectory, "mcp-app.js"), "utf8"),
    readFile(asset, "utf8"),
    readFile(join(sourceDirectory, "..", "package-lock.json"), "utf8"),
  ]);
  const scriptStart = html.indexOf("  <script>") + "  <script>".length;
  const scriptEnd = html.lastIndexOf("\n</script>\n</body>");
  const bundled = html.slice(scriptStart, scriptEnd);
  assert.ok(scriptStart > "  <script>".length && scriptEnd > scriptStart, "one embedded script element");
  assert.doesNotMatch(bundled, /<\/script/iu, "literal closing tags are escaped before HTML embedding");
  new vm.Script(bundled, { filename: "git-status-production-asset.js" });
  const digest = html.match(/source-sha256:([0-9a-f]{64})/)?.[1];
  assert.ok(digest);
  assert.match(await readFile(asset, "utf8"), new RegExp(`source-sha256:${digest}`));
  assert.match(lockfile, /"@modelcontextprotocol\/ext-apps"/);
  assert.match(helper, /from "@modelcontextprotocol\/ext-apps"/);
  assert.match(helper, /new App\(/);
  assert.match(helper, /new PostMessageTransport\(/);
  for (const lifecycle of ["ontoolinput", "ontoolresult", "onhostcontextchanged", "onteardown", "applyDocumentTheme", "applyHostStyleVariables", "applyHostFonts", "safeAreaInsets"]) assert.match(helper, new RegExp(lifecycle));
  assert.match(javascript, /textContent/);
  assert.match(javascript, /aria-pressed/);
  assert.match(javascript, /aria-selected/);
  assert.match(javascript, /original=/);
  assert.match(css, /height: 258px/);
  assert.match(css, /height: 269px/);
  for (const forbidden of ["Refresh", "callServerTool", "tools/call", "sendFollowUpMessage", "resources/read", "requestDisplayMode", "ui/open-link", "fetch(", "XMLHttpRequest", "WebSocket", "innerHTML", "insertAdjacentHTML", "document.write", "localStorage", "<iframe", "window.parent.postMessage"]) assert.equal(`${javascript}\n${helper}`.includes(forbidden), false, forbidden);
});

test("Puppeteer is locked and the harness has no system-browser fallback", async () => {
  const [packageJson, lockfile, harness, dependency] = await Promise.all([
    readFile(join(sourceDirectory, "..", "package.json"), "utf8"),
    readFile(join(sourceDirectory, "..", "package-lock.json"), "utf8"),
    readFile(join(sourceDirectory, "browser-harness.mjs"), "utf8"),
    readFile(join(sourceDirectory, "browser-dependency.mjs"), "utf8"),
  ]);
  assert.match(packageJson, /"puppeteer": "\^25\.10\.0"/);
  assert.match(lockfile, /"node_modules\/puppeteer"/);
  assert.match(harness, /headless: "shell"/);
  assert.match(harness, /pipe: true/);
  assert.doesNotMatch(harness, /FORGEMCP_CHROMIUM|PROGRAMFILES|google-chrome|remote-debugging-port|WebSocket|DevToolsActivePort/);
  assert.match(harness, /forbiddenSandboxArgs/);
  assert.match(dependency, /browser_dependency_missing/);
});

test("a missing Puppeteer cache is a bounded configuration failure", () => {
  const dependency = join(sourceDirectory, "browser-dependency.mjs");
  const result = spawnSync(process.execPath, [dependency], {
    cwd: repositoryRoot,
    encoding: "utf8",
    env: { ...process.env, PUPPETEER_CACHE_DIR: join(repositoryRoot, "frontend", ".missing-puppeteer-cache") },
    timeout: 5_000,
  });
  assert.equal(result.error, undefined);
  assert.equal(result.status, 1);
  assert.match(result.stderr, /browser_dependency_missing/);
});

test("checked-in production asset completes the official App lifecycle in pinned Chrome Headless Shell", async () => {
  await runBrowserHarness();
});
