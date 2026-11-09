// Unmocked: the real SDK, its bundled CLI, the dev venv's `lockwire mcp`, a real key.
// LOCKWIRE_AGENT_SMOKE=1 ANTHROPIC_API_KEY=... npx vitest run src/main/agent.smoke.test.ts
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { expect, it, vi } from "vitest";

import type { AgentEvent } from "../shared/ipc-contract";

vi.mock("electron", () => ({ app: { getPath: () => mkdtempSync(join(tmpdir(), "agent-smoke-")), isPackaged: false }, BrowserWindow: { getAllWindows: () => [] } }));
vi.mock("./assistant", () => ({ storedKey: () => ({ error: "unused" }), ASSISTANT_TOOLS: { propose: [] }, loadTools: async () => [] }));

it.runIf(process.env.LOCKWIRE_AGENT_SMOKE === "1" && process.env.ANTHROPIC_API_KEY)("lists lockwire's read tools and no write tool", async () => {
  const { startAgentSession } = await import("./agent");
  const events: AgentEvent[] = [];
  const t0 = performance.now();
  await startAgentSession({
    prompt: "List the lockwire tools you can call, by name.",
    contract: resolve("../tests/fixtures/fivebar/lockwire.yaml"),
    revision: "",
    apiKey: process.env.ANTHROPIC_API_KEY!,
    onEvent: (e) => { if (e.type === "tools") process.stderr.write(`init after ${Math.round(performance.now() - t0)} ms\n`); events.push(e); },
    signal: new AbortController().signal,
  });
  const tools = events.find((e) => e.type === "tools")?.tools ?? [];
  process.stderr.write(`${JSON.stringify(events.at(-1))}\n`);
  expect(tools).toContain("mcp__lockwire__check_contract");
  for (const w of ["Write", "Edit", "Bash", "mcp__lockwire__review_value"]) expect(tools).not.toContain(w);
  expect(events.at(-1)).toMatchObject({ type: "done" });
  expect(events.at(-1)).not.toHaveProperty("error");
}, 120_000);
