import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(sourceDirectory, "..", "..");
const assetPath = join(repositoryRoot, "src", "forgemcp", "apps", "assets", "git-status.html");
const harnessPath = join(sourceDirectory, "render-harness.html");

function browserExecutable() {
  const configured = process.env.FORGEMCP_CHROMIUM;
  const candidates = [
    configured,
    join(process.env.PROGRAMFILES || "", "Google", "Chrome", "Application", "chrome.exe"),
    join(process.env["PROGRAMFILES(X86)"] || "", "Google", "Chrome", "Application", "chrome.exe"),
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
  ].filter(Boolean);
  return candidates.find((candidate) => isAbsolute(candidate) && existsSync(candidate));
}

function boundedTempPath(path) {
  const root = resolve(tmpdir());
  const candidate = resolve(path);
  assert.notEqual(relative(root, candidate).startsWith(".."), true, `temporary path outside system temp: ${candidate}`);
  return candidate;
}

function onceEvent(target, name) {
  return new Promise((resolveEvent, rejectEvent) => {
    const timeout = setTimeout(() => rejectEvent(new Error(`timed out waiting for ${name}`)), 15_000);
    target.addEventListener(name, (event) => { clearTimeout(timeout); resolveEvent(event); }, { once: true });
  });
}

async function waitForBrowserExit(browser) {
  await Promise.race([
    new Promise((resolveExit) => browser.once("exit", resolveExit)),
    new Promise((resolveDelay) => setTimeout(resolveDelay, 5_000)),
  ]);
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id && this.pending.has(message.id)) {
        const { resolveResponse, rejectResponse } = this.pending.get(message.id);
        this.pending.delete(message.id);
        message.error ? rejectResponse(new Error(message.error.message)) : resolveResponse(message.result);
      }
    });
  }

  command(method, params = {}, sessionId = undefined) {
    const id = this.nextId++;
    this.socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    return new Promise((resolveResponse, rejectResponse) => this.pending.set(id, { resolveResponse, rejectResponse }));
  }
}

async function waitFor(check, description) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (await check()) return;
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 50));
  }
  throw new Error(`timed out waiting for ${description}`);
}

async function main() {
  const executable = browserExecutable();
  if (!executable) throw new Error("capability_absent: Chromium executable not found; set FORGEMCP_CHROMIUM to run the browser harness");
  const [asset, harness] = await Promise.all([readFile(assetPath), readFile(harnessPath)]);
  const server = createServer((request, response) => {
    const body = request.url === "/git-status.html" ? asset : request.url === "/harness.html" ? harness : null;
    response.writeHead(body ? 200 : 404, { "content-type": body === asset ? "text/html; charset=utf-8" : "text/html; charset=utf-8" });
    response.end(body || "not found");
  });
  const profile = boundedTempPath(await mkdtemp(join(tmpdir(), "forgemcp-git-status-chromium-")));
  let browser;
  let browserClosed = false;
  try {
    await new Promise((resolveListen) => server.listen(0, "127.0.0.1", resolveListen));
    const port = server.address().port;
    browser = spawn(executable, ["--headless=new", "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore", windowsHide: true });
    const portFile = join(profile, "DevToolsActivePort");
    await waitFor(async () => { try { return (await readFile(portFile, "utf8")).length > 0; } catch { return false; } }, "Chromium DevTools endpoint");
    const [debugPort] = (await readFile(portFile, "utf8")).trim().split(/\r?\n/);
    const version = await (await fetch(`http://127.0.0.1:${debugPort}/json/version`)).json();
    const socket = new WebSocket(version.webSocketDebuggerUrl);
    await onceEvent(socket, "open");
    const cdp = new Cdp(socket);
    const { targetId } = await cdp.command("Target.createTarget", { url: "about:blank" });
    const attached = await cdp.command("Target.attachToTarget", { targetId, flatten: true });
    const sessionId = attached.sessionId;
    const exceptions = [];
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.sessionId === sessionId && message.method === "Runtime.exceptionThrown") {
        const detail = message.params.exceptionDetails;
        exceptions.push(detail.exception?.description || detail.text);
      }
    });
    await cdp.command("Runtime.enable", {}, sessionId);
    await cdp.command("Page.enable", {}, sessionId);
    await cdp.command("Page.navigate", { url: `http://127.0.0.1:${port}/harness.html` }, sessionId);
    async function evaluate(expression) {
      const result = await cdp.command("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sessionId);
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
      return result.result.value;
    }
    try {
    await waitFor(
        async () => await evaluate("window.__forgemcpHarness?.phase === 'rendered' && document.querySelector('#app').contentDocument?.querySelectorAll('.forge-file').length === 4"),
      "initial tool result render",
    );
    } catch (error) {
      throw new Error(`${error.message}: ${JSON.stringify(await evaluate("(() => { const doc=document.querySelector('#app').contentDocument; return { phase: window.__forgemcpHarness?.phase, app: doc?.querySelector('#app')?.textContent, runtime: doc?.defaultView?.__forgemcpExtApps ? 'runtime-loaded' : 'runtime-missing' }; })()"))}; exceptions=${JSON.stringify(exceptions)}`);
    }
    const initial = await evaluate(`(() => { const doc = document.querySelector('#app').contentDocument; return { rows: doc.querySelectorAll('.forge-file').length, branch: doc.querySelector('.forge-branch-name')?.textContent, selected: doc.querySelector('.forge-file[aria-selected=\"true\"]')?.textContent, pwned: doc.defaultView.__xss_canary }; })()`);
    assert.equal(initial.rows, 4);
    assert.match(initial.branch, /<img src=x/);
    assert.match(initial.selected, /<script>window/);
    assert.equal(initial.pwned, undefined);
    const interaction = await evaluate(`(() => { const doc = document.querySelector('#app').contentDocument; doc.querySelector('.forge-filter.untracked').click(); const filterRows = doc.querySelectorAll('.forge-file').length; doc.querySelector('.forge-file').click(); return { filterRows, detail: doc.querySelector('.forge-detail').textContent, pressed: doc.querySelector('.forge-filter.untracked').getAttribute('aria-pressed') }; })()`);
    assert.equal(interaction.filterRows, 1);
    assert.equal(interaction.pressed, "true");
    assert.match(interaction.detail, /untracked/);
    await evaluate("window.__forgemcpHarness.requestTeardown()");
    try {
      await waitFor(async () => await evaluate("window.__forgemcpHarness?.phase === 'tornDown'"), "official teardown acknowledgement");
    } catch (error) {
      throw new Error(`${error.message}: ${JSON.stringify(await evaluate("window.__forgemcpHarness"))}`);
    }
    assert.deepEqual(exceptions, []);
    await cdp.command("Browser.close");
    await waitForBrowserExit(browser);
    browserClosed = browser.exitCode !== null;
    socket.close();
    console.log(JSON.stringify({ status: "passed", browser: "Chromium", lifecycle: ["connect", "tool-result", "filters", "selection", "teardown"] }));
  } finally {
    if (browser && !browser.killed && !browserClosed) {
      browser.kill();
      await waitForBrowserExit(browser);
    }
    await new Promise((resolveClose) => server.close(resolveClose));
    for (let attempt = 0; attempt < 10; attempt += 1) {
      try { await rm(profile, { recursive: true, force: true, maxRetries: 1, retryDelay: 100 }); break; }
      catch (error) {
        if (attempt === 9) throw error;
        await new Promise((resolveDelay) => setTimeout(resolveDelay, 250));
      }
    }
  }
}

await main();
