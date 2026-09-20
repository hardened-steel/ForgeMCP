import { connectWidget } from "./shared/app.js";
import { workspacePresentation, workspaceValue } from "./workspace-view.js";

await connectWidget({ toolName: "workspace_result", describe: workspacePresentation, renderValue: workspaceValue });
