import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

const reactFixture = resolve(process.cwd(), "../runtime-data/ui-pair-panel-20260914/react18/node_modules");
export default defineConfig({
  plugins: [react()],
  resolve: { alias: [
    { find: /^react-dom(\/.*)?$/, replacement: `${reactFixture}/react-dom$1` },
    { find: /^react(\/.*)?$/, replacement: `${reactFixture}/react$1` },
  ] },
  server: { host: "127.0.0.1", port: 18181, strictPort: true,
    proxy: { "/api": { target: process.env.REQPILOT_API_ORIGIN || "http://127.0.0.1:18080", changeOrigin: true } },
    fs: { allow: [resolve(process.cwd(), "..") ] },
  },
});
