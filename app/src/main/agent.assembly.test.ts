import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

const dir = mkdtempSync(join(tmpdir(), "agent-asm-"));
vi.mock("electron", () => ({ app: { getPath: () => dir, isPackaged: false }, BrowserWindow: { getAllWindows: () => [] }, safeStorage: {} }));
const query = vi.fn();
vi.mock("@anthropic-ai/claude-agent-sdk", () => ({
  query: (...a: unknown[]) => query(...a),
  createSdkMcpServer: (o: object) => ({ type: "sdk", instance: o }),
  tool: (name: string, _d: string, shape: object, handler: unknown) => ({ name, shape, handler }),
}));
const callSidecar = vi.fn(async () => ({ json: async () => ({ commands: [] }) }));
vi.mock("./sidecar", async (orig) => ({ ...(await orig<object>()), callSidecar: (...a: []) => callSidecar(...a) }));

const { PROPOSE_TOOLS, registerAssemblySteps, startAgentSession } = await import("./agent");
const robot = mkdtempSync(join(tmpdir(), "robot-asm-"));
writeFileSync(join(robot, "lockwire.yaml"), "mass: 1\n");
type Handler = (a: unknown) => Promise<{ content: { text: string }[]; isError?: boolean }>;
const events: { type: string }[] = [];
const step = async (input: unknown) => {
  let out: Awaited<ReturnType<Handler>> | undefined;
  query.mockImplementation(async function* ({ options }: { options: { mcpServers: { propose: { instance: { tools: { name: string; handler: Handler }[] } } } } }) {
    out = await options.mcpServers.propose.instance.tools.find((t) => t.name === "render_step")!.handler(input);
    yield { type: "result", subtype: "success", session_id: "s", total_cost_usd: 0, modelUsage: {} };
  });
  await startAgentSession({ prompt: "build steps", contract: join(robot, "lockwire.yaml"), revision: "r", apiKey: "sk-ant-test", onEvent: (e) => events.push(e), signal: new AbortController().signal });
  return out!;
};

describe("build instructions (AGT-05)", () => {
  it("is always in the tool list; a free build says it is part of Pro", async () => {
    expect(PROPOSE_TOOLS).toContain("mcp__propose__render_step");
    expect((await step({})).content[0]!.text).toContain("part of Pro");
  });

  it("Pro: all steps, or one step, come back fenced as data; a missing step is an error", async () => {
    const stepper = vi.fn(async () => ({ steps: ["Step 1: base", "Step 2: arm"] }));
    registerAssemblySteps(stepper);
    const all = await step({});
    expect([stepper.mock.calls[0], all.content[0]!.text]).toEqual([[join(robot, "lockwire.yaml")], expect.stringContaining("Step 1: base\nStep 2: arm")]);
    expect((await step({ step: 2 })).content[0]!.text).not.toContain("Step 1");
    const none = await step({ step: 3 });
    expect([none.isError, none.content[0]!.text]).toEqual([true, expect.stringContaining("no step 3")]);
  });

  it("Pro: the steps also go to the renderer as one steps event, root first, each with its links and new link", async () => {
    const scenes = [{ index: 1, title: "base", links: ["base"], newLink: "base" }, { index: 2, title: "arm", links: ["base", "arm"], newLink: "arm", parts: ["servo s1"] }];
    registerAssemblySteps(async () => ({ steps: ["Step 1: base", "Step 2: arm"], scenes }));
    events.length = 0;
    await step({});
    expect(events.filter((e) => e.type === "steps")).toEqual([{ type: "steps", steps: scenes }]);
  });

  it("Pro: an engine refusal is an error with its words", async () => {
    registerAssemblySteps(async () => ({ refusal: "this robot has no links" }));
    const r = await step({});
    expect([r.isError, r.content[0]!.text]).toEqual([true, expect.stringContaining("no links")]);
  });
});
