import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const directory = dirname(fileURLToPath(import.meta.url));
const source = await Promise.all(["catalog-app.js", "operation-app.js"].map(name => readFile(join(directory, name), "utf8")));
for (const [index, code] of source.entries()) {
  assert.match(code, /textContent/);
  assert.match(code, /connectMcpApp/);
  assert.match(code, /structuredContent/);
  assert.doesNotMatch(code, /fetch\(|XMLHttpRequest|WebSocket|innerHTML|outerHTML|insertAdjacentHTML|document\.write|callServerTool|tools\/call|resources\/read|eval\(|Function\(/);
  assert.match(code, index === 0 ? /forgemcp-cmake-catalog/ : /forgemcp-cmake-operation/);
}
