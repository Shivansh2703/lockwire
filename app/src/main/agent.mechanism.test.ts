// AGT-04: text to CAD (Pro). The sidecar is the REAL engine (one python call per command), the SDK is a recorded transcript.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { beforeAll, describe, expect, it, vi } from "vitest";

const dir = mkdtempSync(join(tmpdir(), "agent-mech-"));
vi.mock("electron", () => ({ app: { getPath: () => dir, isPackaged: false }, BrowserWindow: { getAllWindows: () => [] }, safeStorage: {} }));
const query = vi.fn();
vi.mock("@anthropic-ai/claude-agent-sdk", () => ({
  query: (...a: unknown[]) => query(...a),
  createSdkMcpServer: (o: object) => ({ type: "sdk", instance: o }),
  tool: (name: string, _d: string, shape: object, handler: unknown) => ({ name, shape, handler }),
}));
const ROOT = resolve(process.cwd(), ".."); // vitest runs from app/
const py = (code: string, input = "") => execFileSync("uv", ["run", "--project", ROOT, "python", "-I", "-c", code], { input, encoding: "utf8", cwd: ROOT, maxBuffer: 1 << 26 });
const RUN = "import sys,json\nfrom pydantic import TypeAdapter\nfrom lockwire.serve import dispatch, request_type\nprint(dispatch(TypeAdapter(request_type()).validate_python(json.load(sys.stdin))).model_dump_json())";
const callSidecar = vi.fn(async (path: string, init?: { body?: string }) => ({ json: async () => (path === "/v1/hello" ? { commands: [] } : JSON.parse(py(RUN, init!.body!))) }));
vi.mock("./sidecar", async (orig) => ({ ...(await orig<object>()), callSidecar: (...a: [string, { body?: string }]) => callSidecar(...a) }));
vi.mock("./assistant", async (orig) => ({ ...(await orig<object>()), storedKey: () => ({ key: "k", model: "" }) }));

const { handleAgentStart, isMechanismSpec, mechanismPlaybook, PRO_MECHANISM, PROPOSE_TOOLS, startAgentSession } = await import("./agent");
const { handleApply, recordBatch } = await import("./assistant");
type BatchStep = import("./assistant").BatchStep;

const robot = mkdtempSync(join(tmpdir(), "robot-mech-"));
const contract = join(robot, "lockwire.yaml");
const BLANK = join(ROOT, "tests/fixtures/agt04_blank.yaml"); // a lockfile-stored model with no links yet
const TIPPY = join(ROOT, "tests/fixtures/agt04_tippy.yaml"); // a tricycle that tips: any arm hung on it is NO-GO
const bytes = () => readFileSync(contract, "utf8");
const revision = () => createHash("sha256").update(readFileSync(contract)).digest("hex");
const DESC = "a two-link arm with 30 cm links and an elbow that bends 0 to 120 degrees";
const ARM = {
  dimensions: [{ name: "base_size", value: 0.1 }, { name: "link_w", value: 0.04 }, { name: "link_len", value: 0.3 }, { name: "link_half", value: 0.15 }],
  links: [
    { name: "base", shape: { kind: "box", size: ["base_size", "base_size", "base_size"] } },
    { name: "upper", shape: { kind: "box", size: ["link_w", "link_w", "link_len"], offset: [0, 0, "link_half"] } },
    { name: "fore", shape: { kind: "box", size: ["link_w", "link_w", "link_len"], offset: [0, 0, "link_half"] } },
  ],
  joints: [
    { name: "shoulder", type: "revolute", parent: "base", child: "upper", axis: "y", offset: [0, 0, "base_size"] },
    { name: "elbow", type: "revolute", parent: "upper", child: "fore", axis: "y", offset: [0, 0, "link_len"], limits: { lower: 0, upper: 120, unit: "deg", quote: "0 to 120 degrees" } },
  ],
};
const leftovers = () => readdirSync(dir).filter((f) => /^lockwire-(candidate|template)-/.test(f));
const folder = () => Object.fromEntries(readdirSync(robot).map((f) => [f, readFileSync(join(robot, f), "hex")]));

type Handler = (a: unknown) => Promise<{ content: { text: string }[]; isError?: boolean }>;
type Ev = { type: string; proposal?: { id: string; diff: string }; check?: string };
// The agent session as the SDK streams it: one propose_mechanism call from a recorded transcript.
const call = async (input: unknown, description = DESC, base = BLANK) => {
  copyFileSync(base, contract);
  let out: Awaited<ReturnType<Handler>> | undefined;
  query.mockImplementation(async function* ({ options }: { options: { mcpServers: { propose: { instance: { tools: { name: string; handler: Handler }[] } } } } }) {
    out = await options.mcpServers.propose.instance.tools.find((t) => t.name === "propose_mechanism")!.handler(input);
    yield { type: "result", subtype: "success", session_id: "s", total_cost_usd: 0, modelUsage: {} };
  });
  const events: Ev[] = [];
  await startAgentSession({ prompt: mechanismPlaybook(description), mechanism: description, contract, revision: revision(), apiKey: "k", onEvent: (e) => events.push(e as Ev), signal: new AbortController().signal });
  return { out: out!, cards: events.filter((e) => e.type === "proposal"), said: events.filter((e) => e.type === "text").map((e) => (e as { text?: string }).text) };
};

describe("text to CAD is part of Pro", () => {
  it("a free build answers in one line, spends nothing on the key, and the tool is in the fixed list but only says Pro", async () => {
    copyFileSync(BLANK, contract);
    query.mockClear();
    expect(await handleAgentStart({ contract, text: DESC, revision: revision() })).toEqual({ ok: false, error: PRO_MECHANISM });
    expect(query).not.toHaveBeenCalled();
    expect(PROPOSE_TOOLS).toContain("mcp__propose__propose_mechanism");
    const { out, cards } = await call(ARM);
    expect([cards, out.isError, out.content[0]!.text]).toEqual([[], true, expect.stringContaining(PRO_MECHANISM)]);
  });
});
