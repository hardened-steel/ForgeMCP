import { connectWidget } from "./shared/app.js";
import { toolsetPresentation } from "./shared/presentation.js";

await connectWidget({ toolName: "toolsets_list", describe: toolsetPresentation });
