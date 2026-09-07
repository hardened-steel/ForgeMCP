import { connectWidget } from "./shared/app.js";
import { workspacePresentation } from "./shared/presentation.js";

await connectWidget({ toolName: "workspace_overview", describe: workspacePresentation });
