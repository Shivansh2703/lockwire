import { defineConfig } from "vitest/config";

// Its own file, not electron.vite.config.ts: that one describes three Electron builds.
// jsdom (not "node"): box e2-r adds a React component test (Frame.test.tsx) that needs
// window/document; node-only tests (electron-binary, contrast) run fine under jsdom too,
// since it only adds DOM globals rather than removing node's.
export default defineConfig({
  test: {
    include: ["src/**/*.test.{ts,tsx}", "scripts/**/*.test.ts"],
    environment: "jsdom",
  },
});
