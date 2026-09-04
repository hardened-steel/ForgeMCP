import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = join(sourceDirectory, "..", "..");
const commonDirectory = join(sourceDirectory, "..", "common");
const frontendRoot = join(sourceDirectory, "..");
const targets = [
  ["git-diff-app.js", "git-diff.html"],
  ["git-history-app.js", "git-history.html"],
  ["git-source-history-app.js", "git-source-history.html"],
];
const normalizeText = (value) => value.replace(/\r\n?/g, "\n");
const [templateSource, themeSource, cssSource, helperSource, runtimeSource] = await Promise.all([
  readFile(join(sourceDirectory, "template.html"), "utf8"),
  readFile(join(commonDirectory, "theme.css"), "utf8"),
  readFile(join(sourceDirectory, "git-inspection.css"), "utf8"),
  readFile(join(commonDirectory, "mcp-app.js"), "utf8"),
  readFile(join(frontendRoot, "node_modules", "@modelcontextprotocol", "ext-apps", "dist", "src", "app-with-deps.js"), "utf8"),
]);
const requiredRuntimeExports = ["App", "PostMessageTransport", "applyDocumentTheme", "applyHostFonts", "applyHostStyleVariables"];
const exportBlock = /export\{(?<exports>[\s\S]+)\};?\s*$/u.exec(runtimeSource);
if (!exportBlock?.groups?.exports) throw new Error("official ext-apps runtime has no final export block");
const aliases = Object.fromEntries(requiredRuntimeExports.map((name) => {
  const match = new RegExp(`(?:^|,)\\s*(\\w+) as ${name}(?=,|$)`, "u").exec(exportBlock.groups.exports);
  if (!match) throw new Error(`official ext-apps runtime no longer exports ${name}`);
  return [name, match[1]];
}));
const runtime = normalizeText(runtimeSource.slice(0, exportBlock.index)) + `globalThis.__forgemcpExtApps=Object.freeze({${requiredRuntimeExports.map((name) => `${name}:${aliases[name]}`).join(",")}});\n`;
const helper = normalizeText(helperSource)
  .replace(/import\s*\{[\s\S]*?\}\s*from "@modelcontextprotocol\/ext-apps";\s*/u, () => `const {${requiredRuntimeExports.join(",")}}=globalThis.__forgemcpExtApps;\n`)
  .replace(/export\s+(?=function\s)/gu, "");
for (const [sourceName, assetName] of targets) {
  const appSource = await readFile(join(sourceDirectory, sourceName), "utf8");
  const app = normalizeText(appSource).replace(/import\s*\{\s*connectMcpApp\s*\}\s*from "\.\.\/common\/mcp-app\.js";\s*/u, "");
  const javascript = `${runtime}(()=>{\n${helper}${app}\n})();\n`.replace(/[ \t]+(?=\r?\n)/g, "").replaceAll("</script", "<\\/script");
  const template = normalizeText(templateSource);
  const theme = normalizeText(themeSource);
  const css = normalizeText(cssSource);
  const digest = createHash("sha256").update(template).update(theme).update(css).update(javascript).digest("hex");
  const html = template.replace("/* APP_CSS */", () => `${theme}\n${css}`).replace("/* APP_JS */", () => javascript).replace("<!doctype html>", `<!doctype html><!-- source-sha256:${digest} -->`);
  const output = join(repositoryRoot, "src", "forgemcp", "apps", "assets", assetName);
  if (process.argv.includes("--write")) await writeFile(output, html, "utf8");
  else if (normalizeText(await readFile(output, "utf8")) !== html) throw new Error(`${assetName} is stale; run node frontend/git-inspection-apps/build.mjs --write`);
}
