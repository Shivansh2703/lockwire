import { mkdtempSync, readFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import ARM_POWER from "../renderer/armPower.page.fixture.json";

const dir = mkdtempSync(join(tmpdir(), "assistant-show-"));
const sent: unknown[] = []; // every message main pushed to the page on the show channel
vi.mock("electron", () => ({
  app: { getPath: () => dir },
  BrowserWindow: { getAllWindows: () => [{ isDestroyed: () => false, webContents: { send: (channel: string, m: unknown) => channel === "lockwire:assistant-show" && sent.push(m) } }] },
  safeStorage: { isEncryptionAvailable: () => true, encryptString: (s: string) => Buffer.from(`enc:${s}`), decryptString: (b: Buffer) => b.toString().slice(4) },
}));
vi.mock("./log", () => ({ writeLogLine: () => undefined }));
const callSidecar = vi.fn();
vi.mock("./sidecar", () => ({ callSidecar: (...a: unknown[]) => callSidecar(...a) }));

const { toolRow } = await import("./assistantTools");
const { handleSend, handleSettingsSet, handleShowFinding } = await import("./assistant");
const { SHOW_TOOLS, selectionNote, withShown } = await import("./assistantShow");

const C = "/tmp/robot/lockwire.yaml";
const WRIST = "model/joints/wrist/limits/effort";
// A robot as the engine's page gives it, with no scene file (as the NO-GO examples): the wrist joint and the hand link are drawn, the Pi has no place yet, a rail has no body.
const PAGE = {
  robot_graph: { nodes: [{ id: "joint:wrist", kind: "joint", label: "Wrist" }, { id: "link:hand", kind: "link", label: "Hand" }, { id: "device:dev.pi", kind: "device", label: "Pi" }, { id: "rail:rail.5v", kind: "rail", label: "Rail 5 V" }] },
  cards: [{ title: "Rail 5 V overloaded", quantity: "budgets/bud.power.5v", raw: "", involves: ["rail:rail.5v", "device:dev.pi"] }, { title: "Wrist above stall torque", quantity: WRIST, raw: "", involves: ["joint:wrist", "link:hand", "device:dev.pi"] }],
  joints: [{ name: "wrist" }],
  links: [{ name: "hand", solids: [{ kind: "box" }] }, { name: "frame" }],
  scene: null,
};
const json = (body: unknown) => ({ ok: true, status: 200, json: async () => body });
const fetchMock = vi.fn();
const modelSays = (content: unknown[], stop_reason = "end_turn") => fetchMock.mockResolvedValueOnce(json({ content, stop_reason }));
const body = (i: number) => JSON.parse(fetchMock.mock.calls[i]![1].body);
const ran = () => callSidecar.mock.calls.filter(([path]) => path === "/v1/run").map(([, init]) => JSON.parse(init.body));
const run = (name: string, input: Record<string, unknown>, page: object = PAGE) => toolRow(name)!.run!(input, { contract: C, revision: "r1", engine: async () => ({ payload: page }), served: new Set() });
const send = withShown(handleSend); // as ipc.ts registers it

beforeEach(async () => {
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  sent.length = 0;
  callSidecar.mockReset();
  callSidecar.mockImplementation(async (path: string) => (path === "/v1/hello" ? json({ commands: [] }) : json({ version: "1", cli_line: "lockwire page", exit_code: 0, verdict: null, payload: PAGE, refusal: null })));
  await handleSettingsSet({ key: "sk-ant-secret", model: "m-1", enableRobot: C });
});

describe("the Assistant's view", () => {
  it("the eye on a finding row: a person's click sends the same one message the show-only tool sends, and a finding the robot lacks sends none", async () => {
    expect(await handleShowFinding({ contract: C, finding: WRIST })).toEqual({ ok: true });
    expect(sent).toEqual([{ contract: C, action: "show-only", parts: ["wrist", "hand"], finding: 1 }]);
    expect(await handleShowFinding({ contract: C, finding: "model/joints/elbow/limits/effort" })).toEqual({ ok: false });
    expect(sent).toHaveLength(1);
  });

  it("show tools: each tool sends its one message to the page, reads only the page, holds no proposal and is listed on the turn", async () => {
    const calls = [["select-part", { id: "joint:wrist" }], ["show-only", { finding: WRIST }], ["show-all", {}], ["show-connections", { lens: "power" }], ["open-graph", {}]] as const;
    modelSays(calls.map(([name, input], i) => ({ type: "tool_use", id: `t${i}`, name, input })), "tool_use");
    modelSays([{ type: "text", text: "done" }]);
    const turn = await send({ contract: C, revision: "r1", mode: "Check", text: "why" });
    expect(sent).toEqual([
      { contract: C, action: "select-part", part: "wrist" },
      { contract: C, action: "show-only", parts: ["wrist", "hand"], finding: 1 }, // the Pi is not drawn, so it is not asked for
      { contract: C, action: "show-all" },
      { contract: C, action: "show-connections", lens: "power" },
      { contract: C, action: "open-graph" },
    ]);
    expect(turn.shown).toEqual(["Selected Wrist.", 'Opened "Wrist above stall torque" and narrowed the graph to the 3 parts it involves; the 3D view keeps only: Wrist, Hand.', "Showing every part again, and the whole graph.", "Showing the power connections on the 3D robot.", "Opened the graph."]);
    expect(turn.proposals).toEqual([]); // nothing to sign
    expect(SHOW_TOOLS.map((t) => [t.name, t.kind, t.runs])).toEqual(calls.map(([name]) => [name, "read", "local"]));
    expect(ran()).toEqual(Array(3).fill({ command: "page", args: { contract: C } })); // the only engine calls: a read, no witness, no dry run
    expect(readdirSync(dir)).toEqual(["assistant.json"]); // nothing written but the settings saved in beforeEach
    expect(readFileSync(join(__dirname, "ipc.ts"), "utf8")).toContain('"assistant:send": withShown(handleSend)'); // the handler the page calls is the wrapped one
    const results = body(1).messages.at(-1).content as { content: string; is_error?: boolean }[];
    expect(results.every((r) => r.content.includes("UNTRUSTED_CONTRACT_TEXT") && !r.is_error)).toBe(true); // names come from the file: fenced
  });

  it("show tools: an id the robot lacks, a part with no body, an unknown finding and parts that are not drawn are refused, and nothing reaches the page", async () => {
    const refused = [
      await run("select-part", { id: "joint:nope" }), await run("select-part", { id: "rail:rail.5v" }),
      await run("show-only", { parts: ["joint:wrist", "device:ghost"] }), await run("show-only", { finding: "no/such/path" }), await run("show-only", { finding: 7 }),
      await run("show-only", { parts: ["device:dev.pi"] }), await run("show-only", {}), await run("show-connections", { lens: "health" }),
    ];
    expect(refused.map((r) => r.error)).toEqual(refused.map(() => true));
    expect(refused.map((r) => r.text).join("\n")).toMatch(/"joint:nope"[^]*no body[^]*"device:ghost"[^]*1: Wrist above stall torque \(model\/joints\/wrist\/limits\/effort\)[^]*drawn[^]*lens must be/);
    expect(sent).toEqual([]);
    expect((await run("select-part", { id: "wrist" })).text).toBe("Selected Wrist."); // a bare name that one node has
    const twins = { ...PAGE, robot_graph: { nodes: [{ id: "link:arm", kind: "link", label: "Arm" }, { id: "joint:arm", kind: "joint", label: "Arm" }] } };
    expect((await run("select-part", { id: "arm" }, twins)).text).toContain("More than one part is called \"arm\": give the full id (link:arm, joint:arm)");
    expect((await run("show-only", { finding: 0 })).text).toContain("none of them is drawn on the 3D robot, so the graph is opened and the 3D robot is unchanged"); // the finding still opens; an empty list never blanks the view
    expect(sent.at(-1)).toEqual({ contract: C, action: "show-only", parts: [], finding: 0 });
    const one = { ...PAGE, cards: [{ title: "Hand", quantity: "q", raw: "", involves: ["link:hand"] }] };
    expect((await run("show-only", { finding: 0 }, one)).text).toBe('Opened "Hand" and narrowed the graph to the 1 part it involves; the 3D view keeps only: Hand.');
    await run("show-only", { parts: ["joint:wrist", "link:hand"] }, { ...PAGE, scene: { parts: [{ id: "hand" }] } });
    expect(sent.at(-1)).toEqual({ contract: C, action: "show-only", parts: ["hand"] }); // a robot with a scene has no mesh of its own for a joint
    modelSays([{ type: "text", text: "nothing shown" }]);
    expect(await send({ contract: C, revision: "r1", mode: "Check", text: "hi" })).not.toHaveProperty("shown");
  });

  it("a power finding on the real arm-power page narrows the graph to its own parts and says so in plain words", async () => {
    const r = await run("show-only", { finding: "topology/rails/rail.5v/brownout" }, ARM_POWER);
    expect(r.error).toBeUndefined();
    expect(r.text).toMatch(/^Opened "5 V rail sags to 4\.65 V, Jetson needs 4\.75 V" and narrowed the graph to the 8 parts it involves; /);
    expect(r.text).toContain("none of them is drawn on the 3D robot, so the graph is opened and the 3D robot is unchanged");
    expect(r.text).not.toContain("connections view");
    expect(sent.at(-1)).toMatchObject({ action: "show-only", finding: 0 });
  });

  it("ids are listed once, a finding is matched by its exact quantity path or number only, and each turn lists only its own shown lines", async () => {
    expect((await run("show-only", { parts: ["wrist", "joint:wrist", "wrist"] })).text).toBe("Showing only: Wrist.");
    const raw = { ...PAGE, cards: [{ ...PAGE.cards[1]!, raw: "model joints wrist limits effort exceeds stall" }] };
    for (const f of ["model", "joints wrist limits", "0", 1.5, -1]) expect((await run("show-only", { finding: f }, raw)).error).toBe(true);
    expect((await run("show-only", { finding: 0 }, raw)).error).toBeUndefined();
    expect((await run("show-only", { finding: WRIST }, raw)).error).toBeUndefined();
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    const turnOf = withShown(async (args) => { if ((args as { slow?: boolean }).slow) await gate; await run("show-all", {}); return { text: "", proposals: [], verdict: { word: "unknown", at: "" } }; });
    const slow = turnOf({ slow: true });
    const fast = await turnOf({}); // a second send starts and ends while the first waits
    release();
    expect([(await slow).shown?.length, fast.shown?.length]).toEqual([1, 1]);
  });

  it("selection tags: the selection follows the question as fenced data, nothing is sent when nothing is selected, and the cached prefix does not move", async () => {
    modelSays([{ type: "text", text: "a" }]);
    modelSays([{ type: "text", text: "b" }]);
    const C2 = "/tmp/other/lockwire.yaml"; // its own thread: the first message is the whole conversation
    await handleSettingsSet({ enableRobot: C2 });
    await send({ contract: C2, revision: "r1", mode: "Check", text: "what is this?", context: {} });
    await send({ contract: C2, revision: "r1", mode: "Check", text: "and now?", context: { selection: { id: "joint:wrist", name: "Wrist" }, view: "graph", extra: "dropped" } });
    expect(body(0).messages).toEqual([{ role: "user", content: "what is this?" }]);
    const asked = body(1).messages.at(-1).content as string;
    expect(asked).toMatch(/^and now\?\n\nOn the person's screen now[^]*<<<UNTRUSTED_CONTRACT_TEXT-[^]*\{"selected":\{"id":"joint:wrist","name":"Wrist"\},"view":"graph"\}\nUNTRUSTED_CONTRACT_TEXT-\w+>>>$/);
    expect([body(1).system, body(1).tools]).toEqual([body(0).system, body(0).tools]); // tools and system text: the same bytes with and without a selection
    expect(JSON.stringify([body(1).system, body(1).tools])).not.toContain("joint:wrist");
    const plain = (c: unknown) => selectionNote(c, (t) => t);
    expect([plain(undefined), plain({ selection: {} }), plain({ view: "anything" }), plain({ selection: { id: " " } })]).toEqual(["", "", "", ""]);
    expect(plain({ selection: { id: "x".repeat(500) } }).length).toBeLessThan(400); // a long id is cut
  });
});
