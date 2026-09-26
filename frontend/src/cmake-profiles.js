import { connectWidget } from "./shared/app.js";
import { cmakePresentation, renderCMakeValue } from "./cmake-view.js";

await connectWidget({
  toolName: "cmake_profiles",
  describe: (data) => cmakePresentation(data, "cmake_profiles"),
  renderValue: renderCMakeValue,
});
