import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: "./packages/workbench-ui/src/test/setup.ts",
    include: ["packages/workbench-ui/src/**/*.{test,spec}.{ts,tsx}", "apps/web/src/**/*.{test,spec}.{ts,tsx}"],
    execArgv: ["--no-experimental-webstorage"],
    css: true,
  },
});
