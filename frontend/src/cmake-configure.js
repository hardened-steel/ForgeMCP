import { connectWidget } from "./shared/app.js";
import { cmakePresentation, renderCMakeValue } from "./cmake-view.js";

await connectWidget({
  toolName: "cmake_configure",
  describe: (data) => cmakePresentation(data, "cmake_configure"),
  renderValue: renderCMakeValue,
});
