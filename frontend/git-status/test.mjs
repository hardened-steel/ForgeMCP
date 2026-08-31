import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = join(sourceDirectory, "..", "..");

test("source has a stable built asset digest and no network or unsafe DOM sinks", async () => {
  const [template, css, javascript, html] = await Promise.all([
    readFile(join(sourceDirectory, "template.html"), "utf8"),
    readFile(join(sourceDirectory, "git-status-app.css"), "utf8"),
    readFile(join(sourceDirectory, "git-status-app.js"), "utf8"),
    readFile(join(repositoryRoot, "src", "forgemcp", "apps", "assets", "git-status.html"), "utf8"),
  ]);
  const digest = createHash("sha256").update(template).update(css).update(javascript).digest("hex");
  assert.match(html, new RegExp(`source-sha256:${digest}`));
  assert.match(javascript, /ui\/initialize/);
  assert.match(javascript, /ui\/notifications\/initialized/);
  assert.match(javascript, /ui\/notifications\/tool-input/);
  assert.match(javascript, /ui\/notifications\/tool-result/);
  assert.match(javascript, /ui\/notifications\/tool-cancelled/);
  assert.match(javascript, /ui\/notifications\/host-context-changed/);
  assert.match(javascript, /ui\/notifications\/size-changed/);
  assert.match(javascript, /ui\/resource-teardown/);
  assert.match(javascript, /textContent/);
  assert.match(javascript, /state\.refreshing/);
  assert.match(javascript, /MAX_PENDING_REQUESTS = 2/);
  assert.match(javascript, /status\.files\.length > MAX_FILES/);
  assert.match(javascript, /boundedString\(file\.path, MAX_PATH_LENGTH\)/);
  assert.match(javascript, /name: TOOL_NAME/);
  for (const forbidden of ["innerHTML", "insertAdjacentHTML", "document.write", "localStorage", "fetch(", "XMLHttpRequest", "WebSocket", "eval(", "Function(", "ui/open-link", "clipboard", "<iframe"]) assert.equal(html.includes(forbidden), false, forbidden);
});
