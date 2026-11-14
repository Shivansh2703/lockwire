// AGT-07: the agent wires a robot (Pro). The sidecar is the REAL engine (one python call per command), the SDK is a recorded transcript.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";

const dir = mkdtempSync(join(tmpdir(), "agent-wire-"));
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

const robot = mkdtempSync(join(tmpdir(), "robot-wire-"));
const contract = join(robot, "lockwire.yaml");
// The drone example plus one companion computer (a Jetson, whose model id is its pin card): the drone alone has no second pin-carrying board.
const DRONE = join(ROOT, "tests/fixtures/agt07_drone_companion.yaml");
const revision = () => createHash("sha256").update(readFileSync(contract)).digest("hex");
const FC = "dev.flight-controller.pixhawk-6c";
const FUSE = "ptc.bourns.mf-r050";
const listed = py(`import json\nfrom lockwire.catalog import load_catalog\nprint(json.dumps({"parts":[{"id":p.id,"name":p.name,"label":p.label,"numbers":{k:f"{n.value:g} {n.unit}" for k,n in p.numbers.items()}} for p in load_catalog().parts if p.id=="${FUSE}"]}))`);
const end = (device: string, connector: string, pin: number | string, card?: string) => ({ device, connector, pin, ...(card ? { card } : {}) });
const wire = (id: string, a: number, b: number) => ({ id, from: end(FC, "TELEM2", a, "holybro-pixhawk-6c"), to: end("dev.companion", "J12", b) });
const SERIAL = [wire("w.tx", 2, 10), wire("w.rx", 3, 8), wire("w.gnd", 6, 6)]; // TX to RX, RX to TX, ground to ground
const leftovers = () => readdirSync(dir).filter((f) => /^lockwire-candidate-/.test(f));

type Out = { content: { text: string }[]; isError?: boolean };
type Handler = (a: unknown) => Promise<Out>;
type Ev = { type: string; proposal?: { id: string; diff: string }; check?: string; text?: string };
type Call = [string, unknown];
// The agent session as the SDK streams it: a list_parts call and its result, then each tool call in order.
const session = async (calls: Call[], base = DRONE, withList = true) => {
  copyFileSync(base, contract);
  const outs: Out[] = [];
  query.mockImplementation(async function* ({ options }: { options: { mcpServers: { propose: { instance: { tools: { name: string; handler: Handler }[] } } } } }) {
    if (withList) {
      yield { type: "assistant", message: { content: [{ type: "tool_use", id: "u1", name: "mcp__lockwire__list_parts", input: {} }] } };
      yield { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "u1", content: [{ type: "text", text: listed }] }] } };
    }
    for (const [name, input] of calls) outs.push(await options.mcpServers.propose.instance.tools.find((t) => t.name === name)!.handler(input));
    yield { type: "result", subtype: "success", session_id: "s", total_cost_usd: 0, modelUsage: {} };
  });
  const events: Ev[] = [];
  await startAgentSession({ prompt: "wire the drone's flight controller to its companion computer", contract, revision: revision(), apiKey: "k", onEvent: (e) => events.push(e as Ev), signal: new AbortController().signal });
  return { outs, cards: events.filter((e) => e.type === "proposal"), said: events.filter((e) => e.type === "text").map((e) => e.text) };
};
const stage = (wires = SERIAL, fuse = true): Call[] => [
  ...wires.map((w): Call => ["add_wire", w]),
  ...(fuse ? [["place_fuse", { id: "fuse.telem", protects: "rail.telemetry-port-5v", part: FUSE }] as Call] : []),
  ["run_erc", {}],
];
const verdict = () => (JSON.parse(py(RUN, JSON.stringify({ command: "check", args: { contract } }))) as { verdict: string }).verdict;

describe("wiring is part of Pro", () => {
  it("a free build keeps the three tools in the fixed list and each says it is part of Pro, with no card", async () => {
    expect(PROPOSE_TOOLS).toEqual(expect.arrayContaining(["mcp__propose__add_wire", "mcp__propose__place_fuse", "mcp__propose__run_erc"]));
    const { outs, cards } = await session([["add_wire", SERIAL[0]], ["place_fuse", { id: "f", protects: "w.tx" }], ["run_erc", {}]]);
    expect([cards, outs.map((o) => o.isError), outs.map((o) => o.content[0]!.text.includes("part of Pro"))]).toEqual([[], [true, true, true], [true, true, true]]);
  });
});
