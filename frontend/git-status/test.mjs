import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import vm from "node:vm";
import { Cdp } from "./cdp-client.mjs";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = join(sourceDirectory, "..", "..");
const commonDirectory = join(sourceDirectory, "..", "common");
const asset = join(repositoryRoot, "src", "forgemcp", "apps", "assets", "git-status.html");

class FakeSocket extends EventTarget {
  constructor() { super(); this.sent = []; }
  send(value) { this.sent.push(JSON.parse(value)); }
  message(value) { this.dispatchEvent(new MessageEvent("message", { data: value })); }
}

test("CDP routes out-of-order replies and ignores late request IDs", async () => {
  const socket = new FakeSocket(); const cdp = new Cdp(socket, { requestTimeoutMs: 50 });
  const first = cdp.command("first"); const second = cdp.command("second");
  socket.message(JSON.stringify({ id: 2, result: { order: 2 } })); socket.message(JSON.stringify({ id: 1, result: { order: 1 } }));
  assert.deepEqual(await first, { order: 1 }); assert.deepEqual(await second, { order: 2 });
  const late = cdp.command("late", {}, undefined, { timeoutMs: 5 }); await assert.rejects(late, /timed out/);
  const current = cdp.command("current"); socket.message(JSON.stringify({ id: 3, result: { stale: true } })); socket.message(JSON.stringify({ id: 4, result: { current: true } }));
  assert.deepEqual(await current, { current: true });
});

test("CDP rejects malformed responses and browser disconnects", async () => {
  const malformedSocket = new FakeSocket(); const malformed = new Cdp(malformedSocket, { requestTimeoutMs: 50 }); const pending = malformed.command("malformed"); malformedSocket.message("not-json"); await assert.rejects(pending, /malformed JSON/);
  const closedSocket = new FakeSocket(); const closed = new Cdp(closedSocket, { requestTimeoutMs: 50 }); const disconnected = closed.command("disconnect"); closedSocket.dispatchEvent(new Event("close")); await assert.rejects(disconnected, /disconnected/);
});

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

test("checked-in production asset completes the official App lifecycle in Chromium", (t) => {
  const harness = join(sourceDirectory, "browser-harness.mjs");
  const result = execFileSync(process.execPath, [harness], {
    cwd: repositoryRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
  process.stdout.write(`${result}\n`);
  if (result.includes('"status":"capability_absent"')) {
    t.skip("capability_absent: compatible Chromium browser not found");
    return;
  }
  assert.match(result, /"status":"passed"/, result);
});
