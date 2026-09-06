import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

const widgets = {
  workspace: "workspace-overview",
  process: "process-overview",
  toolsets: "toolsets",
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
