import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const directory = dirname(fileURLToPath(import.meta.url));
const sources = await Promise.all(["git-diff-app.js", "git-history-app.js", "git-source-history-app.js"].map((name) => readFile(join(directory, name), "utf8")));
const authored = sources.join("\n");
for (const forbidden of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "callServerTool", "tools/call", "resources/read", "fetch(", "XMLHttpRequest", "WebSocket", "localStorage", "eval(", "Function(", "window.parent.postMessage"]) {
  if (authored.includes(forbidden)) throw new Error(`forbidden authored API: ${forbidden}`);
}
for (const name of ["forgemcp-git-diff", "forgemcp-git-history", "forgemcp-git-source-history"]) {
  if (!authored.includes(name)) throw new Error(`missing App identity: ${name}`);
}
if (!authored.includes("textContent") || !authored.includes("structuredContent") || !authored.includes("JSON.parse")) throw new Error("result safety/fallback handling is incomplete");
