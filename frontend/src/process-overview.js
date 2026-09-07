import { connectWidget } from "./shared/app.js";
import { processPresentation } from "./shared/presentation.js";

await connectWidget({ toolName: "processes_overview", describe: processPresentation });
