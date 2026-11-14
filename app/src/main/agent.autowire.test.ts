// AGT-08: the agent auto-wires placed parts (Pro). The sidecar is the REAL engine, the SDK is a recorded transcript.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";

const dir = mkdtempSync(join(tmpdir(), "agent-auto-"));
vi.mock("electron", () => ({ app: { getPath: () => dir, isPackaged: false }, BrowserWindow: { getAllWindows: () => [] }, safeStorage: {} }));
const query = vi.fn();
vi.mock("@anthropic-ai/claude-agent-sdk", () => ({
  query: (...a: unknown[]) => query(...a),
  createSdkMcpServer: (o: object) => ({ type: "sdk", instance: o }),
  tool: (name: string, _d: string, shape: object, handler: unknown) => ({ name, shape, handler }),
}));
const ROOT = resolve(process.cwd(), ".."); // vitest runs from app/
const py = (code: string, input = "") => execFileSync("uv", ["run", "--project", ROOT, "python", "-I", "-c", code], { input, encoding: "utf8", cwd: ROOT, maxBuffer: 1 << 26 });
const RUN = "import sys,json\nfrom lockwire import plugins\nplugins.load()\nfrom pydantic import TypeAdapter\nfrom lockwire.serve import dispatch, request_type\nprint(dispatch(TypeAdapter(request_type()).validate_python(json.load(sys.stdin))).model_dump_json())";
const callSidecar = vi.fn(async (path: string, init?: { body?: string }) => ({ json: async () => (path === "/v1/hello" ? { commands: [] } : JSON.parse(py(RUN, init!.body!))) }));
vi.mock("./sidecar", async (orig) => ({ ...(await orig<object>()), callSidecar: (...a: [string, { body?: string }]) => callSidecar(...a) }));
vi.mock("./assistant", async (orig) => ({ ...(await orig<object>()), storedKey: () => ({ key: "k", model: "" }) }));

const { PROPOSE_TOOLS, startAgentSession } = await import("./agent");
const { handleApply } = await import("./assistant");

const contract = join(mkdtempSync(join(tmpdir(), "robot-auto-")), "lockwire.yaml");
const TWO = join(ROOT, "tests/fixtures/agt08_two_companions.yaml"); // the drone with two Jetsons, each with one UART
const revision = () => createHash("sha256").update(readFileSync(contract)).digest("hex");
const FC = "dev.flight-controller.pixhawk-6c";
const link = (id: string, to: string) => ({ id, from: { device: FC, card: "holybro-pixhawk-6c", connector: "TELEM2" }, to: { device: to } });
type Out = { content: { text: string }[]; isError?: boolean };
type Ev = { type: string; proposal?: { id: string; diff: string }; check?: string; text?: string };
const session = async (links: unknown[]) => {
  copyFileSync(TWO, contract);
  const outs: Out[] = [];
  query.mockImplementation(async function* ({ options }: { options: { mcpServers: { propose: { instance: { tools: { name: string; handler: (a: unknown) => Promise<Out> }[] } } } } }) {
    outs.push(await options.mcpServers.propose.instance.tools.find((t) => t.name === "auto_wire")!.handler({ links }));
    yield { type: "result", subtype: "success", session_id: "s", total_cost_usd: 0, modelUsage: {} };
  });
  const events: Ev[] = [];
  await startAgentSession({ prompt: "wire the companions to the flight controller", contract, revision: revision(), apiKey: "k", onEvent: (e) => events.push(e as Ev), signal: new AbortController().signal });
  return { outs, cards: events.filter((e) => e.type === "proposal"), said: events.filter((e) => e.type === "text").map((e) => e.text) };
};

describe("auto-wiring is part of Pro", () => {
  it("a free build keeps auto_wire in the fixed list and it says Pro, with no card", async () => {
    expect(PROPOSE_TOOLS).toContain("mcp__propose__auto_wire");
    const { outs, cards } = await session([link("a", "dev.companion")]);
    expect([cards, outs[0]!.isError, outs[0]!.content[0]!.text.includes("part of Pro")]).toEqual([[], true, true]);
  });
});
