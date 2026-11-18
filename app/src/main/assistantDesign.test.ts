// "agent tools": the free design tools (once the old Agent tab's) on the assistant's own loop. The model is scripted; the engine is the REAL one
// (one python call per command, as in assistantBatch.test.ts); the robot is the bundled rover-param template in a temp folder.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

import snapshot from "./assistant-args.gen.json";

const dir = mkdtempSync(join(tmpdir(), "assistant-design-"));
vi.mock("electron", () => ({
  app: { getPath: () => dir },
  BrowserWindow: { getAllWindows: () => [] },
  safeStorage: { isEncryptionAvailable: () => true, encryptString: (s: string) => Buffer.from(`enc:${s}`), decryptString: (b: Buffer) => b.toString().slice(4) },
}));
vi.mock("./log", () => ({ writeLogLine: () => {} }));
const ROOT = resolve(process.cwd(), ".."); // vitest runs from app/
const py = (code: string, input = "") => execFileSync("uv", ["run", "--project", ROOT, "python", "-I", "-c", code], { input, encoding: "utf8", cwd: ROOT, maxBuffer: 1 << 26 });
const RUN = "import sys,json\nfrom pydantic import TypeAdapter\nfrom lockwire.serve import dispatch, request_type\nprint(dispatch(TypeAdapter(request_type()).validate_python(json.load(sys.stdin))).model_dump_json())";
// One test stands in for an engine tool (parts-match) whose reply carries rows in the catalog's own shape: they must still cite nothing.
const PARTS_ROWS_MARK = "PARTS-ROWS-STAND-IN";
const PARTS_ROWS_REPLY = { version: "1", cli_line: "lockwire from-parts", exit_code: 0, verdict: null, refusal: null, payload: { parts: [{ id: "fict.from-another-tool", name: "Fict pack", label: "cited", numbers: {} }] } };
let failBatch = false; // one test makes the engine's batch-edit throw, to see what a half-way robot proposal leaves behind
vi.mock("./sidecar", async (orig) => ({
  ...(await orig<object>()),
  callSidecar: async (path: string, init?: { body?: string }) => ({ json: async () => (failBatch && init?.body?.includes('"batch-edit"') ? Promise.reject(new Error("boom")) : path === "/v1/hello" ? { commands: Object.entries(snapshot).map(([name, args_schema]) => ({ name, args_schema })) } : init?.body?.includes(PARTS_ROWS_MARK) ? PARTS_ROWS_REPLY : JSON.parse(py(RUN, init!.body!))) }),
}));


const { handleApply, handleSend, handleSettingsSet } = await import("./assistant");
const { registerTreeRanker, shows } = await import("./assistantDesign");

const robot = join(mkdtempSync(join(tmpdir(), "design-")), "robot");
mkdirSync(robot);
const contract = join(robot, "lockwire.yaml");
await handleSettingsSet({ key: "sk-ant-k", model: "m-1", enableRobot: contract });
writeFileSync(join(robot, "servo.pdf"), "%PDF-1.4\n"); // read this conversation
writeFileSync(join(robot, "other.pdf"), "%PDF-1.4\n"); // never read
writeFileSync(join(robot, "..", "outside.pdf"), "%PDF-1.4\n");
beforeEach(() => copyFileSync(join(ROOT, "src/lockwire/examples/rover-param/contract.yaml"), contract));
const bytes = () => readFileSync(contract, "utf8");
/** Every file in the robot's folder, hidden ones too: name and bytes. */
const folderHash = () => createHash("sha256").update(readdirSync(robot).sort().map((n) => `${n}:${createHash("sha256").update(readFileSync(join(robot, n))).digest("hex")}`).join("\n")).digest("hex");
const fetchMock = vi.fn();
vi.stubGlobal("fetch", fetchMock);
const json = (body: unknown) => ({ ok: true, status: 200, json: async () => body });

/** One turn: the model calls each tool in order, then says "done". Returns the turn and what each tool answered the model. */
async function ask(text: string, ...uses: [string, object][]) {
  fetchMock.mockReset();
  uses.forEach(([name, input], i) => fetchMock.mockResolvedValueOnce(json({ content: [{ type: "tool_use", id: `u${i}`, name, input }], stop_reason: "tool_use" })));
  fetchMock.mockResolvedValueOnce(json({ content: [{ type: "text", text: "done" }], stop_reason: "end_turn" }));
  const turn = await handleSend({ contract, revision: createHash("sha256").update(readFileSync(contract)).digest("hex"), mode: "design", text });
  const said = fetchMock.mock.calls.slice(1).map(([, init]) => JSON.stringify(JSON.parse(init.body).messages.at(-1).content[0].content));
  return { turn, said };
}
const PACK = { id: "pack.gensace.3s-5000-60c-short", name: "" };
const num = (name: string, value: number, unit: string, quote?: string, page = 2) => ({ name, value, unit, page, quote });
const part = (numbers: object[], datasheet = "servo.pdf") => ({ datasheet, id: "dev.elbow", family: "fam.servo.smart", model: "XL430-W250", owner: "arm", numbers });

describe("agent tools", () => {
  it("propose-part: a PDF read this conversation becomes ONE card citing page and quote; nothing is written before Apply", async () => {
    const before = bytes();
    const { turn, said } = await ask("add this servo", ["read-file", { path: "servo.pdf" }], ["propose-part", part([num("mass", 57.2, "g", "Weight 57.2 g"), num("stall_torque", 1.5, "N*m", "Stall torque 1.5 N*m at 12 V", 3)])]);
    expect(turn.proposals).toHaveLength(1);
    expect(turn.proposals[0]!.diff).toContain('servo.pdf: mass 57.2 g (p.2: "Weight 57.2 g"); stall_torque 1.5 N*m (p.3: "Stall torque 1.5 N*m at 12 V")');
    expect(turn.proposals[0]!.comparison).toMatchObject({ before: expect.any(String), after: expect.any(String) });
    const unchecked = "Numbers, pages and quotes are as the assistant read them from servo.pdf. They are not checked against the file: check them before you apply.";
    expect(turn.proposals[0]!.note).toBe(unchecked); // the card says what is not checked
    expect(turn.proposals[0]!.diff).not.toContain("Numbers, pages and quotes"); // and the diff is only the diff
    expect(said[1]).toContain(unchecked); // and so does what the model is told
    expect(bytes()).toBe(before);
    expect(await handleApply({ id: turn.proposals[0]!.id, witness: "Ada" })).toEqual({ ok: true }); // the line does not break Apply's diff match
    expect(bytes()).toContain("dev.elbow");
    expect(JSON.parse(fetchMock.mock.calls[0]![1].body).system[0].text).toContain("A part from a datasheet"); // the playbook's title; its text is a guide on demand
    // The next turn no longer holds the PDF: its quotes would come from memory, so it must read the PDF again.
    const later = await ask("now add it as dev.wrist", ["propose-part", { ...part([num("mass", 57.2, "g", "Weight 57.2 g")]), id: "dev.wrist" }]);
    expect([later.turn.proposals, later.said[0]]).toEqual([[], '"Not proposed: read that datasheet again with read-file in this turn, then take every number from the page in front of you."']);
  }, 180_000);

  it("propose-part: a number named weight (any case) is the part's mass; mass and weight that agree are fine, that differ are refused with no card", async () => {
    const decl = async (...ns: object[]) => { const { turn, said } = await ask("add it", ["read-file", { path: "servo.pdf" }], ["propose-part", part(ns)]); return { turn, said }; };
    const m = await decl(num("mass", 57.2, "g", "Weight 57.2 g"));
    const w = await decl(num("Weight", 57.2, "g", "Weight 57.2 g"));
    expect(w.turn.proposals).toHaveLength(1);
    expect(w.turn.proposals[0]!.comparison).toEqual(m.turn.proposals[0]!.comparison); // the same declared mass as one named "mass"
    expect((await decl(num("mass", 57.2, "g", "Weight 57.2 g"), num("WEIGHT", 57.2, "g", "Weight 57.2 g"))).turn.proposals).toHaveLength(1);
    const bad = await decl(num("mass", 57.2, "g", "Weight 57.2 g"), num("weight", 60, "g", "Weight 60 g"));
    expect(bad.turn.proposals).toEqual([]);
    expect(bad.said[1]).toMatch(/\\"mass\\" and \\"weight\\" give different numbers/);
  }, 180_000);

  it("propose-part refuses a number with no quote, a quote without its number, a PDF not read this turn and one outside the folder", async () => {
    const before = bytes();
    const { turn, said } = await ask("add it",
      ["read-file", { path: "servo.pdf" }],
      ["propose-part", part([num("mass", 57.2, "g")])],
      ["propose-part", part([num("mass", 57.2, "g", "Weight 57 g")])],
      ["propose-part", part([num("mass", 57.2, "g", "Weight 57.2 g", 0)])],
      ["propose-part", part([num("mass", 57.2, "g", "Weight 57.2 g")], "other.pdf")],
      ["propose-part", part([num("mass", 57.2, "g", "Weight 57.2 g")], "../outside.pdf")]);
    const quote = '"Not proposed: \\"mass\\" needs its page and a quote from the datasheet that shows the number as printed."';
    expect(said.slice(1)).toEqual([quote, expect.stringMatching(/^"Not proposed: the quote \\"Weight 57 g\\" does not show 57\.2 g as one number followed by that unit/), quote,
      '"Not proposed: read that datasheet again with read-file in this turn, then take every number from the page in front of you."',
      expect.stringMatching(/^"Not proposed: The assistant reads only inside the robot's folder/)]);
    expect([turn.proposals, bytes()]).toEqual([[], before]);
  }, 180_000);

  it("propose-part: the numbers it accepted ground the answer and ride on the card; a refused number, a quote's other number and one the model adds stay unverified", async () => {
    fetchMock.mockReset();
    const uses: [string, object][] = [["read-file", { path: "servo.pdf" }], ["propose-part", part([num("mass", 57.2, "g", "Weight 57.2 g"), num("stall torque at 12 V", 1.5, "N*m", "Stall torque 1.5 N*m at 12 V", 3)])],
      ["propose-part", part([num("stall_current", 2.5, "A", "Stall current 2.4 A at 9 V")])]];
    uses.forEach(([name, input], i) => fetchMock.mockResolvedValueOnce(json({ content: [{ type: "tool_use", id: `u${i}`, name, input }], stop_reason: "tool_use" })));
    fetchMock.mockResolvedValueOnce(json({ content: [{ type: "text", text: "It weighs 57.2 g and stalls at 1.5 N*m. At 12 V it draws 2.5 A, about 80 mm long." }], stop_reason: "end_turn" }));
    const turn = await handleSend({ contract, revision: createHash("sha256").update(readFileSync(contract)).digest("hex"), mode: "design", text: "add it" });
    expect(turn.proposals[0]!.cites).toEqual([{ name: "mass", value: 57.2, unit: "g", page: 2, quote: "Weight 57.2 g" }, { name: "stall torque at 12 V", value: 1.5, unit: "N*m", page: 3, quote: "Stall torque 1.5 N*m at 12 V" }]); // the model's name for a number grounds nothing either
    expect(turn.ungrounded).toEqual(["12 V", "2.5 A", "80 mm"]);
  }, 180_000);

  it("a number must stand alone in the quote with its unit after it", () => {
    const rows: [number, string, string, boolean][] = [
      [12, "g", "Weight 12 kg", false], [2, "kg", "Weight 12 kg", false], [1.2, "kg", "Weight 12 kg", false], [1.5, "kg", "1.50 kg", false], [5, "V", "15 V", false], [12, "kg", "12 kgf", false],
      [12, "kg", "Weight 12 kg", true], [12, "kg", "Weight: 12kg", true], [0.52, "N*m", "Stall torque 0.52 N·m at 5 V", true], [7.4, "V", "Rated voltage 7.4 V", true],
      [200, "g", "1,200 g", false], [1200, "g", "1,200 g", false], [5, "V", "5 V/m", false], [5, "N", "Torque 5 N·m", false], [5, "N", "5 N*m", false],
      [3, "V", "Max 5 V, min 3 V", true], [6, "V", "4.8-6 V", true], [12, "kg", "(12 kg)", true],
    ];
    expect(rows.map(([v, u, q]) => shows(q, v, u))).toEqual(rows.map((r) => r[3]));
  });

  it("propose-power-tree: cards only for parts catalog-parts returned this conversation; nothing is written before Apply", async () => {
    const before = bytes();
    const { turn, said } = await ask("design my power", ["catalog-parts", { query: "3s-5000-60c-short" }],
      ["propose-power-tree", { owner: "power", trees: [{ name: "a", pack: { ...PACK, name: "x" }, rails: [] }] }],
      ["propose-power-tree", { owner: "power", trees: [{ name: "a", pack: { id: "pololu.d24v10f5", name: "Pololu" }, rails: [] }] }]);
    expect(said[1]).toBe('"Not proposed: \\"pack.gensace.3s-5000-60c-short\\" is not a cited part that catalog-parts returned in this conversation, and power trees use only those."');
    expect(said[2]).toContain('\\"pololu.d24v10f5\\" is not a cited part that catalog-parts returned');
    expect(turn.proposals).toEqual([]);
    const name = (JSON.parse(py(RUN, JSON.stringify({ command: "catalog-parts", args: { query: "3s-5000-60c-short" } }))).payload.parts[0].name) as string;
    const ok = await ask("that one", ["propose-power-tree", { owner: "power", trees: [{ name: "a", pack: { ...PACK, name }, rails: [] }] }]);
    expect(ok.turn.proposals.map((p) => p.command)).toEqual(["add"]);
    expect(ok.turn.proposals[0]!.diff).toContain(PACK.id);
    expect(ok.said[0]).not.toContain("Ranked"); // a free build proposes the trees unranked
    expect(bytes()).toBe(before);
    // Pro's ranker registers through the free seam (pro/powerTree.ts); the model hears its line after the cards. Stand-in here: a free test never imports pro/.
    registerTreeRanker((trees) => `Ranked: ${trees.map((t) => `${t.name} ${t.rails.map((r) => r.regulator.efficiency_pct ?? "?").join("/")}`).join("; ")}`);
    const ranked = await ask("rank them", ["propose-power-tree", { owner: "power", trees: [{ name: "a", pack: { ...PACK, name }, rails: [{ vout: 5, iout: 1, regulator: { ...PACK, name, efficiency_pct: 90 } }] }] }]);
    expect(ranked.said[0]).toContain("Ranked: a 90");
    const bare = await ask("just the pack", ["propose-power-tree", { owner: "power", trees: [{ name: "b", pack: { ...PACK, name } }] }]); // no rails: the ranker still gets an array
    expect(bare.said[0]).toContain("Ranked: b ");
  }, 180_000);

  it("propose-robot: the open template sized as ONE card checked GO, payload from the person's words; another template is refused", async () => {
    const before = bytes();
    const sizes = [{ name: "chassis_length", value: 0.42 }];
    const pack = { id: PACK.id, name: JSON.parse(py(RUN, JSON.stringify({ command: "catalog-parts", args: { query: "3s-5000-60c-short" } }))).payload.parts[0].name as string };
    const { turn, said } = await ask("a rover about 42 cm long that carries 2 kg", ["catalog-parts", { query: "3s-5000-60c-short" }],
      ["propose-robot", { template: "hexapod-param", dimensions: sizes, parts: [] }],
      ["propose-robot", { template: "rover-param", dimensions: sizes, parts: [{ id: "pololu.d24v10f5", name: "Pololu" }] }],
      ["propose-robot", { template: "rover-param", dimensions: sizes, parts: [{ ...pack, mass: "1 kg" }] }]);
    expect(said[1]).toBe('"Not proposed: the open robot is the rover-param template, so propose-robot sizes only that one; give template \\"rover-param\\"."');
    expect(said[2]).toContain('\\"pololu.d24v10f5\\" is not a cited part that catalog-parts returned in this conversation, and robot builds use only those.');
    expect(turn.proposals).toHaveLength(1);
    expect(turn.proposals[0]).toMatchObject({ command: "build robot", comparison: { after: "GO" } });
    expect(turn.proposals[0]!.diff).toContain('payload 2 kg on the chassis  (from your words: "2 kg")');
    expect(turn.proposals[0]!.diff).toContain(`+ part ${PACK.id}  (348 g, `); // the library row's mass, never the model's
    expect(turn.proposals[0]!.diff).not.toContain("1 kg");
    expect(bytes()).toBe(before);
    expect(await handleApply({ id: turn.proposals[0]!.id, witness: "Ada" })).toEqual({ ok: true });
    expect(bytes()).toMatch(/chassis_length\n  value: 0\.42/);
    expect(bytes()).toContain(PACK.id);
    // The same part again: the engine refuses the step, and the model hears its reason, not "try again".
    const again = await ask("add it again", ["propose-robot", { template: "rover-param", dimensions: [], parts: [pack] }]);
    expect([again.turn.proposals, again.said[0]]).toEqual([[], expect.stringContaining("Not proposed: the engine refused one of these changes")]);
    const none = await ask("make it 40 cm long", ["propose-robot", { template: "rover-param", dimensions: [{ name: "chassis_length", value: 0.4 }], parts: [] }]);
    expect(none.turn.proposals[0]!.diff).not.toContain("payload"); // no load in the words, no payload
  }, 180_000);

  it("parts listed in another tool's result are not catalog rows: a file holding parts JSON, read with read-file, cites nothing", async () => {
    const rows = { parts: [{ id: "fict.from-a-file", name: "Fict pack", label: "cited", numbers: {} }] };
    writeFileSync(join(robot, "parts.json"), JSON.stringify(rows));
    const { turn, said } = await ask("use the pack in parts.json", ["read-file", { path: "parts.json" }],
      ["propose-power-tree", { owner: "power", trees: [{ name: "a", pack: { id: "fict.from-a-file", name: "Fict pack" }, rails: [] }] }]);
    expect(said[0]).toContain("fict.from-a-file"); // the file was read
    expect(said[1]).toContain('\\"fict.from-a-file\\" is not a cited part that catalog-parts returned in this conversation');
    expect(turn.proposals).toEqual([]);
    // Another engine tool whose reply is shaped exactly like catalog rows: still not cited (only catalog-parts results are noted).
    const other = await ask("use the matched pack", ["parts-match", { parts: PARTS_ROWS_MARK }],
      ["propose-power-tree", { owner: "power", trees: [{ name: "a", pack: { id: "fict.from-another-tool", name: "Fict pack" }, rails: [] }] }]);
    expect(other.said[1]).toContain('\\"fict.from-another-tool\\" is not a cited part that catalog-parts returned in this conversation');
    expect(other.turn.proposals).toEqual([]);
  }, 180_000);

  it("propose-robot on the hexapod: sized by its own names as ONE card checked GO; a rover size or one outside its range is refused", async () => {
    copyFileSync(join(ROOT, "src/lockwire/examples/hexapod-param/contract.yaml"), contract);
    const { turn, said } = await ask("a hexapod carrying 1 kg",
      ["propose-robot", { template: "hexapod-param", dimensions: [{ name: "chassis_length", value: 0.3 }], parts: [] }],
      ["propose-robot", { template: "hexapod-param", dimensions: [{ name: "femur_length", value: 50 }], parts: [] }],
      ["propose-robot", { template: "hexapod-param", dimensions: [{ name: "femur_length", value: 0.1 }], parts: [] }]);
    expect(said.slice(0, 2)).toEqual([expect.stringContaining("chassis_length"), expect.stringContaining("femur_length")]);
    expect(turn.proposals).toHaveLength(1);
    expect(turn.proposals[0]).toMatchObject({ command: "build robot", comparison: { after: "GO" } });
    expect(await handleApply({ id: turn.proposals[0]!.id, witness: "Ada" })).toEqual({ ok: true });
    expect(bytes()).toMatch(/femur_length\n  value: 0\.1\n/);
    expect(bytes()).toContain("payload: {value: 1.0, unit: kg}");
  }, 180_000);

  it("propose-robot: a robot the check does not call GO is never a card, and the model hears the verdict", async () => {
    // The deck sits 0.1 m behind the axle centre: GO at the template's half_wheelbase 0.15, tips at 0.05 (agent.robot.test.ts).
    writeFileSync(contract, bytes().replace("      - chassis_height\n    material:", "      - chassis_height\n      origin: {xyz: [-0.1, 0, 0]}\n    material:"));
    const { turn, said } = await ask("make it shorter", ["propose-robot", { template: "rover-param", dimensions: [{ name: "half_wheelbase", value: 0.05 }], parts: [] }]);
    expect([turn.proposals, said[0]]).toEqual([[], expect.stringContaining("Not proposed: Lockwire's check of this robot says NO-GO")]);
  }, 180_000);

  it("a robot proposal whose step throws half-way leaves no copy or temp file beside the lock and the lock's bytes unchanged", async () => {
    const [bytesBefore, listBefore] = [bytes(), readdirSync(robot).sort()];
    const name = JSON.parse(py(RUN, JSON.stringify({ command: "catalog-parts", args: { query: "3s-5000-60c-short" } }))).payload.parts[0].name as string;
    failBatch = true;
    try {
      const { turn, said } = await ask("size it", ["catalog-parts", { query: "3s-5000-60c-short" }], ["propose-robot", { template: "rover-param", dimensions: [{ name: "chassis_length", value: 0.42 }], parts: [{ ...PACK, name }] }]);
      expect([turn.proposals, said[1]]).toEqual([[], expect.stringContaining("could not check this robot")]);
    } finally {
      failBatch = false;
    }
    expect([bytes(), readdirSync(robot).sort()]).toEqual([bytesBefore, listBefore]);
  }, 180_000);

  it("a power-tree part the engine refuses reaches the model as the engine's words and makes no card", async () => {
    const name = JSON.parse(py(RUN, JSON.stringify({ command: "catalog-parts", args: { query: "3s-5000-60c-short" } }))).payload.parts[0].name as string;
    const tree = { owner: "power", trees: [{ name: "a", pack: { ...PACK, name }, rails: [] }] };
    const first = await ask("pack", ["catalog-parts", { query: "3s-5000-60c-short" }], ["propose-power-tree", tree]);
    await handleApply({ id: first.turn.proposals[0]!.id, witness: "Ada" });
    const again = await ask("same pack again", ["propose-power-tree", tree]);
    expect(again.turn.proposals).toEqual([]);
    expect(again.said[0]).toContain("1 part(s) were refused: device 'pack.gensace.3s-5000-60c-short' already exists"); // the engine's own words
    expect(again.said[0]).not.toMatch(/try again/i);
  }, 180_000);

  it("the robot's folder is byte-identical around a successful power-tree call and a successful robot call, before Apply", async () => {
    const name = JSON.parse(py(RUN, JSON.stringify({ command: "catalog-parts", args: { query: "3s-5000-60c-short" } }))).payload.parts[0].name as string;
    const h0 = folderHash();
    const a = await ask("pack", ["catalog-parts", { query: "3s-5000-60c-short" }], ["propose-power-tree", { owner: "power", trees: [{ name: "a", pack: { ...PACK, name }, rails: [] }] }]);
    const h1 = folderHash();
    const b = await ask("size", ["propose-robot", { template: "rover-param", dimensions: [{ name: "chassis_length", value: 0.42 }], parts: [] }]);
    expect([a.turn.proposals.length, b.turn.proposals.length, h1, folderHash()]).toEqual([1, 1, h0, h0]);
  }, 180_000);
});
