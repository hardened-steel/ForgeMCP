import { connectWidget } from "./shared/app.js";
import { clangdPresentation, clangdValue } from "./clangd-view.js";

await connectWidget({ toolName: "clangd", describe: clangdPresentation, renderValue: clangdValue });
