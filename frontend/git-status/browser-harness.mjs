import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Cdp } from "./cdp-client.mjs";

const REQUEST_TIMEOUT_MS = 15_000;
const HTTP_TIMEOUT_MS = 5_000;
const EXIT_TIMEOUT_MS = 5_000;
const MAX_BROWSER_OUTPUT_BYTES = 64 * 1024;
const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(sourceDirectory, "..", "..");
const assetPath = join(repositoryRoot, "src", "forgemcp", "apps", "assets", "git-status.html");
const harnessPath = join(sourceDirectory, "render-harness.html");

function phase(name) { console.log(JSON.stringify({ phase: name, timestamp: new Date().toISOString() })); }
function delay(ms) { return new Promise((resolveDelay) => setTimeout(resolveDelay, ms)); }
function browserExecutable() {
  const candidates = [process.env.FORGEMCP_CHROMIUM, join(process.env.PROGRAMFILES || "", "Google", "Chrome", "Application", "chrome.exe"), join(process.env["PROGRAMFILES(X86)"] || "", "Google", "Chrome", "Application", "chrome.exe"), "/usr/bin/google-chrome", "/usr/bin/chromium"].filter(Boolean);
  return candidates.find((candidate) => isAbsolute(candidate) && existsSync(candidate));
}
function boundedTempPath(path) {
  const root = resolve(tmpdir()); const candidate = resolve(path);
  assert.notEqual(relative(root, candidate).startsWith(".."), true, `temporary path outside system temp: ${candidate}`);
  return candidate;
}
function onceEvent(target, name, timeoutMs = REQUEST_TIMEOUT_MS) {
  return new Promise((resolveEvent, rejectEvent) => {
    const timeout = setTimeout(() => rejectEvent(new Error(`timed out waiting for ${name}`)), timeoutMs);
    const done = (event) => { clearTimeout(timeout); resolveEvent(event); };
    if (typeof target.addEventListener === "function") target.addEventListener(name, done, { once: true });
    else target.once(name, done);
  });
}
async function waitForBrowserExit(browser) {
  await Promise.race([new Promise((resolveExit) => browser.once("exit", resolveExit)), delay(EXIT_TIMEOUT_MS)]);
}
function boundedOutput(stream) {
  let text = ""; stream.setEncoding("utf8"); stream.on("data", (chunk) => { text = `${text}${chunk}`.slice(-MAX_BROWSER_OUTPUT_BYTES); }); return () => text;
}
async function terminateCreatedBrowser(browser) {
  if (!browser || browser.exitCode !== null) return;
  browser.kill(); await waitForBrowserExit(browser);
  if (browser.exitCode !== null || process.platform !== "win32" || !browser.pid) return;
  await Promise.race([new Promise((resolveExit) => { const killer = spawn("taskkill.exe", ["/pid", String(browser.pid), "/t", "/f"], { stdio: "ignore", windowsHide: true }); killer.once("exit", resolveExit); }), delay(EXIT_TIMEOUT_MS)]);
  await waitForBrowserExit(browser);
}
async function waitFor(check, description) {
  const deadline = Date.now() + REQUEST_TIMEOUT_MS;
  while (Date.now() < deadline) { if (await check()) return; await delay(50); }
  throw new Error(`timed out waiting for ${description}`);
}
async function fetchJson(url) {
  const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), HTTP_TIMEOUT_MS);
  try { const response = await fetch(url, { signal: controller.signal }); if (!response.ok) throw new Error(`CDP endpoint returned HTTP ${response.status}`); return await response.json(); }
  finally { clearTimeout(timer); }
}

async function main() {
  phase("chromium-launch");
  const executable = browserExecutable();
  if (!executable) {
    console.log(JSON.stringify({ status: "capability_absent", reason: "compatible Chromium browser not found" }));
    return;
  }
  const [asset, harness] = await Promise.all([readFile(assetPath), readFile(harnessPath)]);
  const server = createServer((request, response) => { const body = request.url === "/git-status.html" ? asset : request.url === "/harness.html" ? harness : null; response.writeHead(body ? 200 : 404, { "content-type": "text/html; charset=utf-8" }); response.end(body || "not found"); });
  const profile = boundedTempPath(await mkdtemp(join(tmpdir(), "forgemcp-git-status-chromium-")));
  let browser; let browserClosed = false; let stderr = () => ""; let browserExit = "not observed";
  try {
    await Promise.race([new Promise((resolveListen, rejectListen) => { server.once("error", rejectListen); server.listen(0, "127.0.0.1", resolveListen); }), delay(REQUEST_TIMEOUT_MS).then(() => { throw new Error("timed out starting harness HTTP server"); })]);
    const port = server.address().port;
    browser = spawn(executable, ["--headless=new", "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    boundedOutput(browser.stdout); stderr = boundedOutput(browser.stderr); browser.once("exit", (code, signal) => { browserExit = `code=${code}; signal=${signal}`; }); await onceEvent(browser, "spawn");
    phase("cdp-connect");
    const portFile = join(profile, "DevToolsActivePort");
    await waitFor(async () => { try { return (await readFile(portFile, "utf8")).length > 0; } catch { return false; } }, "Chromium DevTools endpoint");
    const [debugPort] = (await readFile(portFile, "utf8")).trim().split(/\r?\n/);
    const version = await fetchJson(`http://127.0.0.1:${debugPort}/json/version`);
    const socket = new WebSocket(version.webSocketDebuggerUrl); await onceEvent(socket, "open"); const cdp = new Cdp(socket);
    phase("target-create");
    const { targetId } = await cdp.command("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await cdp.command("Target.attachToTarget", { targetId, flatten: true });
    const exceptions = [];
    socket.addEventListener("message", (event) => { try { const message = JSON.parse(String(event.data)); if (message.sessionId === sessionId && message.method === "Runtime.exceptionThrown") { const detail = message.params.exceptionDetails; exceptions.push(detail.exception?.description || detail.text); } } catch { /* Cdp reports malformed protocol data. */ } });
    // Runtime.evaluate and Page.navigate are request/response commands; no
    // event-domain subscription is needed for this deterministic harness.
    phase("page-load"); await cdp.command("Page.navigate", { url: `http://127.0.0.1:${port}/harness.html` }, sessionId);
    async function evaluate(expression) { const result = await cdp.command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sessionId); if (result.exceptionDetails) throw new Error(result.exceptionDetails.text); return result.result.value; }
    phase("app-initialize");
    await waitFor(async () => await evaluate("window.__forgemcpHarness?.phase === 'rendered' && document.querySelector('#app').contentDocument?.querySelectorAll('.forge-file').length === 4"), "initial tool result render");
    phase("tool-result");
    const initial = await evaluate(`(() => { const doc = document.querySelector('#app').contentDocument; return { rows: doc.querySelectorAll('.forge-file').length, branch: doc.querySelector('.forge-branch-name')?.textContent, selected: doc.querySelector('.forge-file[aria-selected=\\"true\\"]')?.textContent, pwned: doc.defaultView.__xss_canary }; })()`);
    assert.equal(initial.rows, 4); assert.match(initial.branch, /<img src=x/); assert.match(initial.selected, /<script>window/); assert.equal(initial.pwned, undefined);
    phase("interaction");
    const interaction = await evaluate(`(() => { const doc = document.querySelector('#app').contentDocument; doc.querySelector('.forge-filter.untracked').click(); const filterRows = doc.querySelectorAll('.forge-file').length; doc.querySelector('.forge-file').click(); return { filterRows, detail: doc.querySelector('.forge-detail').textContent, pressed: doc.querySelector('.forge-filter.untracked').getAttribute('aria-pressed') }; })()`);
    assert.equal(interaction.filterRows, 1); assert.equal(interaction.pressed, "true"); assert.match(interaction.detail, /untracked/);
    phase("teardown"); await evaluate("window.__forgemcpHarness.requestTeardown()"); await waitFor(async () => await evaluate("window.__forgemcpHarness?.phase === 'tornDown'"), "official teardown acknowledgement");
    assert.deepEqual(exceptions, []);
    phase("browser-exit"); await cdp.command("Browser.close"); await waitForBrowserExit(browser); browserClosed = browser.exitCode !== null; cdp.close(); socket.close();
    console.log(JSON.stringify({ status: "passed", browser: "Chromium", lifecycle: ["connect", "tool-result", "filters", "selection", "teardown"] }));
  } catch (error) {
    throw new Error(`${error instanceof Error ? error.message : String(error)}; Chromium exit: ${browserExit}; Chromium stderr: ${stderr()}`);
  } finally {
    if (browser && !browserClosed) await terminateCreatedBrowser(browser);
    await new Promise((resolveClose) => server.close(resolveClose));
    for (let attempt = 0; attempt < 10; attempt += 1) { try { await rm(profile, { recursive: true, force: true, maxRetries: 1, retryDelay: 100 }); break; } catch (error) { if (attempt === 9) throw new Error(`failed to remove test-owned Chromium profile: ${error.message}; browser stderr: ${stderr()}`); await delay(250); } }
  }
}
try {
  await main();
} catch (error) {
  throw error instanceof Error ? error : new Error(String(error));
}
