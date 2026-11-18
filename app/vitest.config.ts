import { defineConfig } from "vitest/config";

// Its own file, not electron.vite.config.ts: that one describes three Electron builds.
// jsdom (not "node"): React component tests (Frame.test.tsx) need
// window/document; node-only tests (electron-binary, contrast) run fine under jsdom too,
// since it only adds DOM globals rather than removing node's.
//
// Two projects: the hidden real-app files (*.e2e.test.ts) launch the built app and time out
// when they share the machine with the parallel unit files. `groupOrder` runs the e2e group
// only after the unit group has finished; `fileParallelism: false` runs its files one by one.
const shared = {
  // The Assistant's read-guide bundles Lockwire's own docs/guides pages, which sit one folder above app/.
  server: { fs: { allow: [".."] } },
};

// CI and check:free run `vitest run --exclude ...` to leave the e2e files and build.test.ts out.
// Projects ignore a CLI --exclude (measured: `vitest list` still lists build.test.ts), so for
// that call keep the old single block, which honours it and already has no e2e files to order.
const cliExcludes = process.argv.includes("--exclude");

export default defineConfig(cliExcludes ? {
  ...shared,
  test: {
    include: ["src/**/*.test.{ts,tsx}", "scripts/**/*.test.ts"],
    environment: "jsdom",
  },
} : {
  test: {
    projects: [
      {
        ...shared,
        test: {
          name: "unit",
          include: ["src/**/*.test.{ts,tsx}", "scripts/**/*.test.ts"],
          exclude: ["**/node_modules/**", "**/*.e2e.test.ts"],
          environment: "jsdom",
          sequence: { groupOrder: 0 },
        },
      },
      {
        ...shared,
        test: {
          name: "e2e",
          include: ["src/**/*.e2e.test.ts"],
          environment: "jsdom",
          fileParallelism: false,
          sequence: { groupOrder: 1 },
        },
      },
    ],
  },
});
