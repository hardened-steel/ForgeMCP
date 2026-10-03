import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

const widgets = {
  workspace: "workspace-tree",
  file: "workspace-file",
  search: "workspace-search",
  result: "workspace-result",
  process: "process-overview",
  "process-details": "process-details",
  toolsets: "toolsets",
  "cmake-profiles": "cmake-profiles",
  "cmake-configure": "cmake-configure",
  "cmake-build": "cmake-build",
  "cmake-test": "cmake-test",
  clangd: "clangd-result",
};

export default defineConfig(({ mode }) => {
  const widget = widgets[mode] ?? widgets.workspace;
  return {
    plugins: [viteSingleFile()],
    build: {
      outDir: fileURLToPath(new URL("../src/forgemcp/assets", import.meta.url)),
      emptyOutDir: mode === "workspace",
      rollupOptions: {
        input: fileURLToPath(new URL(`${widget}.html`, import.meta.url)),
      },
    },
  };
});
