import { connectWidget } from "./shared/app.js";
import { cmakePresentation, renderCMakeValue } from "./cmake-view.js";

await connectWidget({
  toolName: "cmake_test",
  describe: (data) => cmakePresentation(data, "cmake_test"),
  renderValue: renderCMakeValue,
});
