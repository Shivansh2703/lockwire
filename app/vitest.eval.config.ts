import { defineConfig } from "vitest/config";

// Only for the paid Assistant eval (src/main/assistant-eval/live.eval.ts); the normal config never includes it.
// The whole run is one test, so testTimeout caps the whole run. The default reporter and silent: false print each question's line as it
// is scored, in any terminal (a minimal reporter would hold a passing test's lines back), so a timeout loses no line already paid for.
export default defineConfig({ server: { fs: { allow: [".."] } }, test: { include: ["src/main/assistant-eval/live.eval.ts"], environment: "node", testTimeout: 1_800_000, hookTimeout: 120_000, reporters: ["default"], silent: false } });
