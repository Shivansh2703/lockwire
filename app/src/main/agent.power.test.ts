import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

const dir = mkdtempSync(join(tmpdir(), "agent-pwr-"));
vi.mock("electron", () => ({ app: { getPath: () => dir, isPackaged: false }, BrowserWindow: { getAllWindows: () => [] }, safeStorage: {} }));
const query = vi.fn();
vi.mock("@anthropic-ai/claude-agent-sdk", () => ({
  query: (...a: unknown[]) => query(...a),
  createSdkMcpServer: (o: object) => ({ type: "sdk", instance: o }),
  tool: (name: string, _d: string, shape: object, handler: unknown) => ({ name, shape, handler }),
}));
const json = (body: unknown) => ({ json: async () => body });
const callSidecar = vi.fn(async (path: string) => {
  if (path === "/v1/hello") return json({ commands: [{ name: "add", args_schema: { type: "object", properties: { contract: {}, witness: {} }, required: [] } }] });
  return json({ version: "1", cli_line: "lockwire", exit_code: 0, verdict: null, payload: { diff: "+device: x\n" }, refusal: null });
});
vi.mock("./sidecar", async (orig) => ({ ...(await orig<object>()), callSidecar: (...a: [string]) => callSidecar(...a) }));

vi.mock("./assistant", async (orig) => ({ ...(await orig<object>()), storedKey: () => ({ key: "k", model: "" }) }));

const { handlePowerDesign, PROPOSE_TOOLS, powerPlaybook, powerTreeParts, recordListParts, startAgentSession } = await import("./agent");

// Fictional parts shaped like F4b rows: a URL source, the id/name/family a list_parts row carries.
const src = (id: string) => `https://example.test/${id} (spec table), read 2026-11-13`;
const mk = (id: string, family: string, efficiency_pct?: number) => ({ id, name: `Fictional ${id}`, family, source: src(id), ...(efficiency_pct ? { efficiency_pct } : {}) });
const pack = mk("fict.pack4s", "fam.battery.pack");
const trees = [
  { name: "Two bucks", pack, rails: [{ vout: 5, iout: 3, regulator: mk("fict.buck5", "fam.power.buck", 90) }, { vout: 12, iout: 1, regulator: mk("fict.buck12", "fam.power.buck", 90) }] },
  { name: "Buck and LDO", pack, rails: [{ vout: 5, iout: 3, regulator: mk("fict.buck5b", "fam.power.buck", 80) }, { vout: 12, iout: 1, regulator: mk("fict.ldo12", "fam.power.ldo", 50) }] },
];

const robot = mkdtempSync(join(tmpdir(), "robot-pwr-"));
writeFileSync(join(robot, "lockwire.yaml"), "mass: 1\n");
type Handler = (a: unknown) => Promise<{ content: { text: string }[]; isError?: boolean }>;
const rows = (ps: ReturnType<typeof mk>[], label = "cited") => JSON.stringify({ parts: ps.map((p) => ({ id: p.id, name: p.name, label, numbers: {} })), cli_line: "lockwire catalog parts" });
const allParts = [pack, ...trees.flatMap((t) => t.rails.map((r) => r.regulator))];
// The agent session as the SDK streams it: a list_parts tool_use, its tool_result, then the propose call.
const call = async (input: unknown, listed: string | null = rows(allParts), tool = "mcp__lockwire__list_parts") => {
  let out: Awaited<ReturnType<Handler>> | undefined;
  query.mockImplementation(async function* ({ options }: { options: { mcpServers: { propose: { instance: { tools: { name: string; handler: Handler }[] } } } } }) {
    if (listed !== null) {
      yield { type: "assistant", message: { content: [{ type: "tool_use", id: "u1", name: tool, input: {} }] } };
      yield { type: "user", message: { content: [{ type: "tool_result", tool_use_id: "u1", content: [{ type: "text", text: listed }] }] } };
    }
    out = await options.mcpServers.propose.instance.tools.find((t) => t.name === "propose_power_tree")!.handler(input);
    yield { type: "result", subtype: "success", session_id: "s", total_cost_usd: 0, modelUsage: {} };
  });
  const events: { type: string; proposal?: { args: Record<string, unknown> } }[] = [];
  await startAgentSession({ prompt: "4S, 5 V 3 A, 12 V 1 A", contract: join(robot, "lockwire.yaml"), revision: "r1", apiKey: "k", onEvent: (e) => events.push(e as never), signal: new AbortController().signal });
  return { out: out!, events: events.filter((e) => e.type === "proposal") };
};

describe("power trees from a spec", () => {
  it("'4S, 5 V 3 A, 12 V 1 A' gives two cited trees as cards, one per distinct part, none written", async () => {
    const { out, events } = await call({ owner: "robot", trees });
    expect(events.map((e) => e.proposal!.args.id)).toEqual(["fict.pack4s", "fict.buck5", "fict.buck12", "fict.buck5b", "fict.ldo12"]);
    expect(events.map((e) => e.proposal!.args.source)).toContain("Lockwire parts library: fict.buck5 (cited in the catalog)");
    expect(out.content[0]!.text).toContain("awaits the user's signature");
  });

  it("one uncited part refuses the whole call in one sentence and records no card", async () => {
    const listed = JSON.stringify({ parts: allParts.map((p) => ({ id: p.id, name: p.name, label: p.id === "fict.buck5" ? "unverified" : "cited" })) });
    const { out, events } = await call({ owner: "robot", trees }, listed);
    expect(out.isError).toBe(true);
    expect(out.content[0]!.text).toContain('"fict.buck5" is not a cited part');
    expect(events).toEqual([]);
  });

  it("the card's source and family come from the library row, never the model's URL or family", async () => {
    const lie = { ...mk("fict.buck5", "fam.made.up"), source: "https://evil.test/fake.pdf" };
    const { events } = await call({ owner: "robot", trees: [{ ...trees[0]!, rails: [{ vout: 5, iout: 3, regulator: lie }] }] });
    const card = events.find((e) => e.proposal!.args.id === "fict.buck5")!.proposal!.args;
    expect(card).toMatchObject({ fields: { family: "fam.unclassified", model: "Fictional fict.buck5" }, source: "Lockwire parts library: fict.buck5 (cited in the catalog)" });
    expect(JSON.stringify(events)).not.toContain("evil.test");
  });

  it("an invented part id with a real-looking URL is refused, though list_parts returned other parts", async () => {
    const fake = [{ ...trees[0]!, rails: [{ vout: 5, iout: 3, regulator: mk("fict.invented", "fam.power.buck", 95) }] }];
    const { out, events } = await call({ owner: "robot", trees: fake });
    expect(out.isError).toBe(true);
    expect(out.content[0]!.text).toContain('"fict.invented" is not a cited part');
    expect(events).toEqual([]);
  });

  it("a part seen in a list_parts result is accepted; none seen, an unverified label or a renamed part is not", async () => {
    expect((await call({ owner: "robot", trees: [trees[0]] }, rows(allParts))).events).toHaveLength(3);
    expect((await call({ owner: "robot", trees: [trees[0]] }, null)).out.isError).toBe(true);
    expect((await call({ owner: "robot", trees: [trees[0]] }, rows(allParts, "unverified"))).out.isError).toBe(true);
    const seen = new Map();
    recordListParts(seen, rows([{ ...pack, name: "Other name" }]));
    expect(powerTreeParts([trees[0]!], seen)).toHaveProperty("refusal");
  });

  it("a dynamixel library id still gets the neutral family, never one a keyword check reads", async () => {
    const dxl = { id: "dynamixel.xl430-w250", name: "Dynamixel XL430-W250" };
    const tree = [{ name: "t", pack: dxl, rails: [] }];
    const { events } = await call({ owner: "robot", trees: tree }, JSON.stringify({ parts: [{ ...dxl, label: "cited" }] }));
    expect(events.map((e) => (e.proposal!.args.fields as { family: string }).family)).toEqual(["fam.unclassified"]);
  });

  it("parts JSON in another tool's result is not a list_parts row: refused, no cards", async () => {
    const { out, events } = await call({ owner: "robot", trees }, rows(allParts), "mcp__lockwire__describe_device");
    expect(out.isError).toBe(true);
    expect(events).toEqual([]);
  });

  it("agent:power-design starts the agent on the playbook with the spec in it", async () => {
    let prompt = "";
    query.mockImplementation(async function* (a: { prompt: string }) { prompt = a.prompt; yield { type: "result", subtype: "success", session_id: "s2", total_cost_usd: 0, modelUsage: {} }; });
    expect(await handlePowerDesign({ contract: join(robot, "lockwire.yaml"), revision: "r1", spec: "3S, 5 V 2 A" })).toEqual({ ok: true });
    await vi.waitFor(() => expect(prompt).toBe(powerPlaybook("3S, 5 V 2 A")));
    expect(prompt).toContain("propose_power_tree");
  });

  it("free builds say ranking is Pro; the tool is in the fixed list and the spec rides in the message", async () => {
    expect((await call({ owner: "robot", trees })).out.content[0]!.text).toContain("part of Pro");
    expect(PROPOSE_TOOLS).toContain("mcp__propose__propose_power_tree");
    expect(powerPlaybook("4S, 5 V 3 A")).toContain("4S, 5 V 3 A");
  });

  it("a free build gives the model no ranking at all: no Ranked line, no watts", async () => {
    expect((await call({ owner: "robot", trees })).out.content[0]!.text).not.toMatch(/Ranked|\d W\b/);
  });
});
