import { defineConfig } from "vite";
import { hostUiContract } from "./src/host/contract";
import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";
// @ts-expect-error Build-time stylesheet projection is shared JavaScript.
import { scopePanelCss } from "./scripts/scope-panel-css.mjs";

const sharedAnimations = [...new Set(["src/styles/index.css", "src/styles/workbench.css"].flatMap(path =>
  [...readFileSync(path, "utf8").matchAll(/@(?:-webkit-)?keyframes\s+([\w-]+)/g)].map(match => match[1])))];

export default defineConfig({
  plugins: [react(), {
    name: 'reqpilot-host-contract',
    generateBundle(_options, bundle) {
      const entry = Object.values(bundle).find(item => item.type === 'chunk' && item.isEntry);
      if (!entry || entry.type !== 'chunk' || hostUiContract.exports.some(name => !entry.exports.includes(name))) throw new Error('Shared host contract references a missing public export.');
      this.emitFile({ type: 'asset', fileName: 'host-contract.json', source: JSON.stringify(hostUiContract, null, 2) + '\n' });
    },
  }],
  css: { postcss: { plugins: [scopePanelCss(sharedAnimations)] } },
  build: {
    outDir: "dist-panel", emptyOutDir: true, cssCodeSplit: false,
    lib: { entry: "src/host/client.ts", formats: ["es", "cjs"], fileName: format => `reqpilotx-readonly.${format === "es" ? "js" : "cjs"}`, cssFileName: "reqpilotx-readonly" },
    rolldownOptions: { external: ["react", "react-dom", "react/jsx-runtime", "react/jsx-dev-runtime", "react-dom/client"] },
  },
});
