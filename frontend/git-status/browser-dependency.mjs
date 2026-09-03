import { constants } from "node:fs";
import { access } from "node:fs/promises";
import puppeteer from "puppeteer";
import { PUPPETEER_REVISIONS } from "puppeteer-core/internal/revisions.js";

export const BROWSER_PRODUCT = "chrome-headless-shell";
export const BROWSER_REVISION = PUPPETEER_REVISIONS[BROWSER_PRODUCT];

export async function resolvePinnedBrowser() {
  let executablePath;
  try {
    executablePath = await puppeteer.executablePath({ headless: "shell" });
    await access(executablePath, constants.X_OK);
  } catch {
    throw new Error(`browser_dependency_missing; product=${BROWSER_PRODUCT}; revision=${BROWSER_REVISION}`);
  }
  return { product: BROWSER_PRODUCT, revision: BROWSER_REVISION, executablePath };
}

if (process.argv[1] && new URL(`file://${process.argv[1].replaceAll("\\", "/")}`).href === import.meta.url) {
  try {
    console.log(JSON.stringify({ status: "available", ...(await resolvePinnedBrowser()) }));
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}
