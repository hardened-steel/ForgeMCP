import { connectWidget } from "./shared/app.js";
import { cmakePresentation, renderCMakeValue } from "./cmake-view.js";

await connectWidget({
  toolName: "cmake_build",
  describe: (data) => cmakePresentation(data, "cmake_build"),
  renderValue: renderCMakeValue,
});
