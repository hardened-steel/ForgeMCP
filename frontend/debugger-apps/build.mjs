import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = join(sourceDirectory, "..", "..");
const commonDirectory = join(sourceDirectory, "..", "common");
const frontendRoot = join(sourceDirectory, "..");
const normalize = (value) => value.replace(/\r\n?/g, "\n");
const views = [
  ["Debugger Session", "debugger-session-app.js", "debugger-session.html"],
  ["Debugger Stack", "debugger-stack-app.js", "debugger-stack.html"],
  ["Debugger Data", "debugger-data-app.js", "debugger-data.html"],
];
if (!process.argv.includes("--write") && !process.argv.includes("--check")) throw new Error("Use --write or --check.");
const [template, theme, css, helper, runtimeSource] = await Promise.all([
  readFile(join(sourceDirectory, "template.html"), "utf8"), readFile(join(commonDirectory, "theme.css"), "utf8"), readFile(join(sourceDirectory, "debugger-app.css"), "utf8"), readFile(join(commonDirectory, "mcp-app.js"), "utf8"), readFile(join(frontendRoot, "node_modules", "@modelcontextprotocol", "ext-apps", "dist", "src", "app-with-deps.js"), "utf8"),
]);
const exportsNeeded = ["App", "PostMessageTransport", "applyDocumentTheme", "applyHostFonts", "applyHostStyleVariables"];
const exportBlock = /export\{(?<exports>[\s\S]+)\};?\s*$/u.exec(runtimeSource);
if (!exportBlock?.groups?.exports) throw new Error("official ext-apps runtime has no final export block");
const aliases = Object.fromEntries(exportsNeeded.map((name) => { const found = new RegExp(`(?:^|,)\\s*(\\w+) as ${name}(?=,|$)`, "u").exec(exportBlock.groups.exports); if (!found) throw new Error(`official ext-apps runtime no longer exports ${name}`); return [name, found[1]]; }));
const runtime = normalize(runtimeSource.slice(0, exportBlock.index)) + `globalThis.__forgemcpExtApps=Object.freeze({${exportsNeeded.map((name) => `${name}:${aliases[name]}`).join(",")}});\n`;
const cleanHelper = normalize(helper).replace(/import\s*\{[\s\S]*?\}\s*from "@modelcontextprotocol\/ext-apps";\s*/u, () => `const {${exportsNeeded.join(",")}}=globalThis.__forgemcpExtApps;\n`).replace(/export\s+(?=function\s)/gu, "");
for (const [title, sourceName, outputName] of views) {
  const source = await readFile(join(sourceDirectory, sourceName), "utf8");
  const app = normalize(source).replace(/import\s*\{\s*connectMcpApp\s*\}\s*from "\.\.\/common\/mcp-app\.js";\s*/u, "");
  const javascript = `${runtime}(()=>{\n${cleanHelper}${app}\n})();\n`.replace(/[ \t]+(?=\r?\n)/g, "").replaceAll("</script", "<\\/script");
  const digest = createHash("sha256").update(normalize(template)).update(normalize(theme)).update(normalize(css)).update(javascript).digest("hex");
  const html = normalize(template).replace("/* APP_TITLE */", title).replace("/* APP_CSS */", () => `${normalize(theme)}\n${normalize(css)}`).replace("/* APP_JS */", () => javascript).replace("<!doctype html>", `<!doctype html><!-- source-sha256:${digest} -->`);
  const output = join(repositoryRoot, "src", "forgemcp", "apps", "assets", outputName);
  if (process.argv.includes("--write")) await writeFile(output, html, "utf8");
  if (process.argv.includes("--check") && normalize(await readFile(output, "utf8")) !== html) throw new Error(`${outputName} is stale; run build.mjs --write`);
}
