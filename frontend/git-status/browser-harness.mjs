import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";
import { resolvePinnedBrowser } from "./browser-dependency.mjs";

const TIMEOUT_MS = 30_000;
const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(sourceDirectory, "..", "..");
const assetPath = join(repositoryRoot, "src", "forgemcp", "apps", "assets", "git-status.html");
const harnessPath = join(sourceDirectory, "render-harness.html");

let currentPhase = "not-started";
function phase(name) { currentPhase = name; console.log(JSON.stringify({ phase: name, timestamp: new Date().toISOString() })); }
function boundedTempPath(path) {
  const root = resolve(tmpdir()); const candidate = resolve(path);
  assert.equal(relative(root, candidate).startsWith(".."), false, `temporary path outside system temp: ${candidate}`);
  return candidate;
}
function withTimeout(action, description) {
  let timer;
  return Promise.race([
    action,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`timeout: ${description}`)), TIMEOUT_MS); }),
  ]).finally(() => clearTimeout(timer));
}
async function waitForMainFrame(page) {
  const deadline = Date.now() + TIMEOUT_MS;
  while (Date.now() < deadline) {
    try {
      if (page.mainFrame()) return;
    } catch { /* Chrome has not dispatched the initial target yet. */ }
    await new Promise((resolveWait) => setTimeout(resolveWait, 25));
  }
  throw new Error("timeout: waiting for Puppeteer main-frame lifecycle");
}
function browserFailureCategory(error) {
  const message = error instanceof Error ? error.message : String(error);
  if (message.includes("browser_dependency_missing")) return "browser_dependency_missing";
  if (message.includes("timeout:")) return "timeout";
  return "browser_harness_error";
}
const forbiddenSandboxArgs = ["--no" + "-sandbox", "--disable" + "-setuid-sandbox"];
function stateResult(kind) {
  const base = {
    repository: "available", git_available: true, git_configured: true, branch: "main", detached: false, unborn: false,
    ahead: 0, behind: 0, staged_count: 0, unstaged_count: 0, untracked_count: 0, conflicted_count: 0,
    incomplete: false, truncated: false, files: [],
  };
  if (kind === "error") base.repository = "error";
  if (kind === "incomplete") base.incomplete = true;
  if (kind === "truncated") base.truncated = true;
  return { structuredContent: base };
}
async function panelMetrics(frame) {
  return await frame.evaluate(() => {
    const panel = document.querySelector(".forge-term");
    if (!panel) return null;
    const bounds = panel.getBoundingClientRect();
    return { height: bounds.height, scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth };
  });
}
async function assertStableViewport(page, frame, width, expectedHeight) {
  await page.setViewport({ width, height: 400, deviceScaleFactor: 1 });
  await frame.waitForFunction(() => document.querySelector(".forge-term"));
  const metrics = await panelMetrics(frame);
  assert.ok(metrics);
  assert.equal(metrics.height, expectedHeight, `unexpected panel height at ${width}px`);
  assert.ok(metrics.scrollWidth <= metrics.clientWidth, `horizontal overflow at ${width}px`);
}

export async function runBrowserHarness() {
  phase("browser-dependency");
  const pinned = await resolvePinnedBrowser();
  const [asset, harness] = await Promise.all([readFile(assetPath), readFile(harnessPath)]);
  const server = createServer((request, response) => {
    const body = request.url === "/git-status.html" ? asset : request.url === "/harness.html" ? harness : null;
    response.writeHead(body ? 200 : 404, { "content-type": "text/html; charset=utf-8" });
    response.end(body || "not found");
  });
  const profile = boundedTempPath(await mkdtemp(join(tmpdir(), "forgemcp-git-status-puppeteer-")));
  const failures = [];
  let browser;
  let expectedDisconnect = false;
  try {
    await withTimeout(new Promise((resolveListen, rejectListen) => { server.once("error", rejectListen); server.listen(0, "127.0.0.1", resolveListen); }), "starting local harness HTTP server");
    const port = server.address().port;
    const localOrigin = `http://127.0.0.1:${port}`;
    const allowed = new Set([`${localOrigin}/git-status.html`]);
    phase("puppeteer-launch");
    browser = await puppeteer.launch({
      headless: "shell", pipe: true, userDataDir: profile, timeout: TIMEOUT_MS,
      args: ["--disable-gpu", "--mute-audio", "--hide-scrollbars", "--window-size=736,400", "--disable-background-networking"],
    });
    const launchArgs = browser.process()?.spawnargs || [];
    for (const forbiddenArg of forbiddenSandboxArgs) assert.equal(launchArgs.includes(forbiddenArg), false, "browser sandbox must remain enabled");
    assert.ok(launchArgs.some((argument) => argument.includes("remote-debugging-pipe")), "Puppeteer must use its pipe transport");
    browser.on("disconnected", () => { if (!expectedDisconnect) failures.push("browser disconnected unexpectedly"); });
    const pages = await browser.pages();
    const page = pages[0] || await browser.newPage();
    // Puppeteer resolves the page handle before Chrome's initial about:blank
    // target is necessarily dispatched to the FrameManager. This is a bounded
    // lifecycle wait, not a fixed delay or test retry.
    await waitForMainFrame(page);
    page.setDefaultTimeout(TIMEOUT_MS);
    page.on("pageerror", (error) => failures.push(`pageerror: ${error.message}`));
    page.on("console", (message) => { if (message.type() === "error") failures.push(`console error: ${message.text()}`); });
    page.on("request", (request) => { if (!allowed.has(request.url())) failures.push(`unexpected network request: ${request.url()}`); });
    page.on("requestfailed", (request) => failures.push(`failed local resource: ${request.url()}`));
    page.on("framenavigated", (navigatedFrame) => {
      if (navigatedFrame.parentFrame() === null) failures.push(`unexpected navigation: ${navigatedFrame.url()}`);
      else if (!allowed.has(navigatedFrame.url())) failures.push(`unexpected frame navigation: ${navigatedFrame.url()}`);
    });
    phase("page-load");
    await page.setContent(harness.toString("utf8").replace("<head>", `<head><base href="${localOrigin}/">`), { waitUntil: "load", timeout: TIMEOUT_MS });
    const iframe = await page.waitForSelector("#app");
    const frame = await iframe.contentFrame();
    assert.ok(frame, "App iframe must be present");
    phase("app-initialize");
    await frame.waitForFunction(() => document.querySelectorAll(".forge-file").length === 4);
    const initial = await frame.evaluate(() => ({
      rows: document.querySelectorAll(".forge-file").length,
      branch: document.querySelector(".forge-branch-name")?.textContent,
      selected: document.querySelector(".forge-file[aria-selected='true']")?.textContent,
      pwned: globalThis.__xss_canary,
    }));
    assert.equal(initial.rows, 4); assert.match(initial.branch || "", /<img src=x/); assert.match(initial.selected || "", /<script>window/); assert.equal(initial.pwned, undefined);
    phase("dimensions");
    await assertStableViewport(page, frame, 736, 258);
    await assertStableViewport(page, frame, 360, 269);
    await assertStableViewport(page, frame, 320, 269);
    phase("interaction");
    await frame.click(".forge-filter.untracked");
    assert.equal((await frame.$$(".forge-file")).length, 1);
    assert.equal(await frame.$eval(".forge-filter.untracked", (node) => node.getAttribute("aria-pressed")), "true");
    await frame.click(".forge-file");
    assert.match(await frame.$eval(".forge-detail", (node) => node.textContent || ""), /untracked/);
    await assertStableViewport(page, frame, 320, 269);
    await frame.click(".forge-filter.untracked");
    assert.equal((await frame.$$(".forge-file")).length, 4, "removing a filter restores all rows");
    await assertStableViewport(page, frame, 736, 258);
    for (const kind of ["clean", "error", "incomplete", "truncated"]) {
      phase(`state-${kind}`);
      await page.evaluate((result) => window.__forgemcpHarness.publishResult(result), stateResult(kind));
      const selector = kind === "error" ? ".forge-error" : `.forge-${kind === "clean" ? "complete" : kind}`;
      await frame.waitForSelector(selector);
      await assertStableViewport(page, frame, 736, 258);
      await assertStableViewport(page, frame, 320, 269);
    }
    assert.deepEqual(failures, [], failures.join("\n"));
    phase("teardown");
    await page.evaluate(() => window.__forgemcpHarness.requestTeardown());
    await page.waitForFunction(() => window.__forgemcpHarness?.phase === "tornDown");
    assert.deepEqual(failures, [], failures.join("\n"));
    phase("browser-close");
    expectedDisconnect = true;
    await browser.close();
    browser = undefined;
    console.log(JSON.stringify({ status: "passed", browser: pinned, lifecycle: ["connect", "tool-result", "filters", "selection", "teardown"] }));
  } catch (error) {
    throw new Error(`browser_harness_failure; category=${browserFailureCategory(error)}; phase=${currentPhase}; ${error instanceof Error ? error.message : String(error)}`);
  } finally {
    if (browser) { expectedDisconnect = true; await browser.close().catch(() => {}); }
    if (server.listening) await new Promise((resolveClose) => server.close(resolveClose));
    await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await runBrowserHarness();
