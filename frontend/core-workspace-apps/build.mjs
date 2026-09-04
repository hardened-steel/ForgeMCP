import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = join(sourceDirectory, "..", "..");
const commonDirectory = join(sourceDirectory, "..", "common");
const frontendRoot = join(sourceDirectory, "..");
const requiredRuntimeExports = ["App", "PostMessageTransport", "applyDocumentTheme", "applyHostFonts", "applyHostStyleVariables"];
const normalizeText = (text) => text.replace(/\r\n?/g, "\n");
const mode = process.argv.includes("--write") ? "write" : process.argv.includes("--check") ? "check" : null;
if (mode === null || process.argv.length !== 3) throw new Error("Use exactly one of --write or --check.");

const [themeSource, helperSource, runtimeSource] = await Promise.all([
  readFile(join(commonDirectory, "theme.css"), "utf8"),
  readFile(join(commonDirectory, "mcp-app.js"), "utf8"),
  readFile(join(frontendRoot, "node_modules", "@modelcontextprotocol", "ext-apps", "dist", "src", "app-with-deps.js"), "utf8"),
]);
const exportBlock = /export\{(?<exports>[\s\S]+)\};?\s*$/u.exec(runtimeSource);
if (!exportBlock?.groups?.exports) throw new Error("official ext-apps runtime has no final export block");
const aliases = Object.fromEntries(requiredRuntimeExports.map((name) => {
  const match = new RegExp(`(?:^|,)\\s*(\\w+) as ${name}(?=,|$)`, "u").exec(exportBlock.groups.exports);
  if (!match) throw new Error(`official ext-apps runtime no longer exports ${name}`);
  return [name, match[1]];
}));
const runtime = normalizeText(runtimeSource.slice(0, exportBlock.index)) + `globalThis.__forgemcpExtApps=Object.freeze({${requiredRuntimeExports.map((name) => `${name}:${aliases[name]}`).join(",")}});\n`;
const helper = normalizeText(helperSource).replace(/import\s*\{[\s\S]*?\}\s*from "@modelcontextprotocol\/ext-apps";\s*/u, () => `const {${requiredRuntimeExports.join(",")}}=globalThis.__forgemcpExtApps;\n`).replace(/export\s+(?=function\s)/gu, "");

async function buildAsset({ template, css, app, output }) {
  const [templateSource, cssSource, appSource] = await Promise.all([
    readFile(join(sourceDirectory, template), "utf8"),
    readFile(join(sourceDirectory, css), "utf8"),
    readFile(join(sourceDirectory, app), "utf8"),
  ]);
  const source = normalizeText(appSource).replace(/import\s*\{\s*connectMcpApp\s*\}\s*from "\.\.\/common\/mcp-app\.js";\s*/u, "");
  const javascript = `${runtime}(()=>{\n${helper}${source}\n})();\n`.replace(/[ \t]+(?=\r?\n)/g, "").replaceAll("</script", "<\\/script");
  const digest = createHash("sha256").update(normalizeText(templateSource)).update(normalizeText(themeSource)).update(normalizeText(cssSource)).update(javascript).digest("hex");
  const html = normalizeText(templateSource).replace("/* APP_CSS */", () => `${normalizeText(themeSource)}\n${normalizeText(cssSource)}`).replace("/* APP_JS */", () => javascript).replace("<!doctype html>", `<!doctype html><!-- source-sha256:${digest} -->`);
  const target = join(repositoryRoot, "src", "forgemcp", "apps", "assets", output);
  if (mode === "write") await writeFile(target, html, "utf8");
  else if (normalizeText(await readFile(target, "utf8")) !== html) throw new Error(`${output} is stale; run node frontend/core-workspace-apps/build.mjs --write`);
}

await Promise.all([
  buildAsset({ template: "server-status-template.html", css: "server-status-app.css", app: "server-status-app.js", output: "server-status.html" }),
  buildAsset({ template: "workspace-result-template.html", css: "workspace-result-app.css", app: "workspace-result-app.js", output: "workspace-result.html" }),
]);
