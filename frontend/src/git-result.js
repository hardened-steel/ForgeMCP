import { connectWidget } from "./shared/app.js";
import { gitPresentation, renderGitValue } from "./git-view.js";
import "./git.css";

await connectWidget({
  toolName: "git_status",
  describe: (data, resources, toolName) => gitPresentation(data, toolName),
  renderValue: renderGitValue,
});
