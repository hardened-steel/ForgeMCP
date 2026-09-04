import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = join(sourceDirectory, "..", "..");
const output = join(repositoryRoot, "src", "forgemcp", "apps", "assets", "git-status.html");
const commonDirectory = join(sourceDirectory, "..", "common");
const frontendRoot = join(sourceDirectory, "..");
function normalizeText(text) {
  return text.replace(/\r\n?/g, "\n");
}

const [templateSource, themeSource, cssSource, helperSource, appSource, runtimeSource] = await Promise.all([
  readFile(join(sourceDirectory, "template.html"), "utf8"),
  readFile(join(commonDirectory, "theme.css"), "utf8"),
  readFile(join(sourceDirectory, "git-status-app.css"), "utf8"),
  readFile(join(commonDirectory, "mcp-app.js"), "utf8"),
  readFile(join(sourceDirectory, "git-status-app.js"), "utf8"),
  // The official distribution is itself a single, dependency-inclusive ESM
  // artifact.  Reusing it avoids a platform-native bundler subprocess while
  // keeping the production App on the official ext-apps runtime.
  readFile(join(frontendRoot, "node_modules", "@modelcontextprotocol", "ext-apps", "dist", "src", "app-with-deps.js"), "utf8"),
]);
const template = normalizeText(templateSource);
const theme = normalizeText(themeSource);
const css = normalizeText(cssSource);
const requiredRuntimeExports = ["App", "PostMessageTransport", "applyDocumentTheme", "applyHostFonts", "applyHostStyleVariables"];
const exportBlock = /export\{(?<exports>[\s\S]+)\};?\s*$/u.exec(runtimeSource);
if (!exportBlock?.groups?.exports) throw new Error("official ext-apps runtime has no final export block");
const aliases = Object.fromEntries(requiredRuntimeExports.map((name) => {
  const match = new RegExp(`(?:^|,)\\s*(\\w+) as ${name}(?=,|$)`, "u").exec(exportBlock.groups.exports);
  if (!match) throw new Error(`official ext-apps runtime no longer exports ${name}`);
  return [name, match[1]];
}));
const runtime = normalizeText(runtimeSource.slice(0, exportBlock.index))
  + `globalThis.__forgemcpExtApps=Object.freeze({${requiredRuntimeExports.map((name) => `${name}:${aliases[name]}`).join(",")}});\n`;
const helper = normalizeText(helperSource)
  .replace(
    /import\s*\{[\s\S]*?\}\s*from "@modelcontextprotocol\/ext-apps";\s*/u,
    () => `const {${requiredRuntimeExports.join(",")}}=globalThis.__forgemcpExtApps;\n`,
  )
  .replace(/export\s+(?=function\s)/gu, "");
const app = normalizeText(appSource).replace(
  /import\s*\{\s*connectMcpApp\s*\}\s*from "\.\.\/common\/mcp-app\.js";\s*/u,
  "",
);
// Keep the generated source free of trailing whitespace so the checked-in
// package asset also passes the repository's diff hygiene gate.
const javascript = `${runtime}(()=>{\n${helper}${app}\n})();\n`
  .replace(/[ \t]+(?=\r?\n)/g, "")
  // A dependency may contain a literal closing script tag.  One backslash is
  // valid JavaScript and prevents the HTML parser from closing this element.
  .replaceAll("</script", "<\\/script");
const digest = createHash("sha256").update(template).update(theme).update(css).update(javascript).digest("hex");
const html = template
  // Functional replacements keep `$&`, `$\`` and `$'` inside dependency code
  // literal.  Passing the bundle as a replacement string corrupts its source.
  .replace("/* APP_CSS */", () => `${theme}\n${css}`)
  .replace("/* APP_JS */", () => javascript)
  .replace("<!doctype html>", `<!doctype html><!-- source-sha256:${digest} -->`);
if (process.argv.includes("--write")) {
  await writeFile(output, html, "utf8");
} else {
  const existing = await readFile(output, "utf8");
  if (normalizeText(existing) !== html) throw new Error("git-status.html is stale; run npm run write:asset --prefix frontend and commit the regenerated asset");
}
