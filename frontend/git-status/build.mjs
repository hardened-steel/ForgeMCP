import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const sourceDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = join(sourceDirectory, "..", "..");
const output = join(repositoryRoot, "src", "forgemcp", "apps", "assets", "git-status.html");
const [template, css, javascript] = await Promise.all([
  readFile(join(sourceDirectory, "template.html"), "utf8"),
  readFile(join(sourceDirectory, "git-status-app.css"), "utf8"),
  readFile(join(sourceDirectory, "git-status-app.js"), "utf8"),
]);
const digest = createHash("sha256").update(template).update(css).update(javascript).digest("hex");
const html = template
  .replace("/* APP_CSS */", css)
  .replace("/* APP_JS */", javascript)
  .replace("<!doctype html>", `<!doctype html><!-- source-sha256:${digest} -->`);
await writeFile(output, html, "utf8");
