import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
// @ts-expect-error Build helper is a Node ESM module, outside browser sources.
import { stampWebBuild } from "../../scripts/web-build-identity.mjs";

export default defineConfig({
  plugins: [react(), { name: "reqpilotx-build-identity", apply: "build", enforce: "post",
    writeBundle(options) { stampWebBuild(options.dir || "dist"); } }],
  base: "/",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
    manifest: true,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.REQPILOT_API_ORIGIN || "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
  },
});
