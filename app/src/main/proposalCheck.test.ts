// A card's before and after. The engine here is a small stand-in that reads and writes whatever file it is handed, as the real one does:
// a "robot" is {limit, draw} and its check is NO-GO while draw is over limit. So a wrong file in any call shows up in the result or on disk.
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, rmSync, mkdtempSync, readdirSync, readFileSync, symlinkSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const dir = mkdtempSync(join(tmpdir(), "card-"));
vi.mock("electron", () => ({ app: { getPath: () => tmpdir() }, BrowserWindow: { getAllWindows: () => [] }, safeStorage: {} }));
vi.mock("./log", () => ({ writeLogLine: () => {} }));
let down = false; // the engine cannot check
let stall = false; // the engine's check never answers
// A model file beside the lock (as a URDF beside a real one): its `extra` adds to the draw the check sees. The lock's bytes never hold it.
const MODEL = () => join(dir, "model.json");
type Robot = Record<string, number>;
const edit = (r: Robot, c: { path?: string; value?: unknown; fields?: Robot }): Robot => (c.fields ? { ...r, draw: r.draw! + c.fields.draw! } : { ...r, [c.path!]: Number(c.value) });
const engine = (command: string, a: { contract: string; dry_run?: boolean; changes?: object[]; expected_revision?: string }) => {
  const robot = JSON.parse(readFileSync(a.contract, "utf8")) as Robot;
  if (command === "check") {
    if (down) throw new Error("engine down");
    const model = join(dirname(a.contract), "model.json");
    if (existsSync(model)) robot.draw! += (JSON.parse(readFileSync(model, "utf8")) as Robot).extra!;
    const over = robot.draw! > robot.limit!;
    return { verdict: over ? "NO-GO" : "GO", exit_code: over ? 1 : 0, findings: over ? [{ severity: "violation", quantity: "budgets/servo", observed: `${robot.draw} W`, expected: `<= ${robot.limit} W` }] : [] };
  }
  if (command === "swap-apply" && "witness" in a) return { refusal: "swap-apply takes no witness" }; // as the real engine: extra fields are refused
  if (a.dry_run) return { diff: "-old\n+new\n" };
  if (a.expected_revision && a.expected_revision !== createHash("sha256").update(readFileSync(a.contract)).digest("hex")) return { refusal: "the robot changed since this was proposed (revision)", exit_code: 2 }; // as the real engine
  writeFileSync(a.contract, JSON.stringify(command === "swap-apply" ? { ...robot, draw: 10 } : (a.changes ?? [a]).reduce(edit, robot))); // a swap: the stronger part clears the over-limit finding
  if (!a.changes) writeFileSync(join(dirname(a.contract), `.${basename(a.contract)}.lockwire-undo.json`), "{}"); // the real engine's undo journal, beside the file it wrote
  return {};
};
const SWAP = { additionalProperties: false, properties: Object.fromEntries(["contract", "joint", "part_id", "expected_revision", "dry_run"].map((k) => [k, {}])) }; // the served swap-apply: no witness
const props = Object.fromEntries(["contract", "witness", "expected_revision", "dry_run", "path", "value", "kind", "fields", "source"].map((k) => [k, {}]));
vi.mock("./sidecar", () => ({
  callSidecar: async (path: string, init?: { body?: string }) => {
    if (path === "/v1/hello") return { json: async () => ({ commands: [...["declare", "add"].map((name) => ({ name, args_schema: { properties: props } })), { name: "swap-apply", args_schema: SWAP }] }) };
    const body = JSON.parse(init!.body!);
    if (stall && body.command === "check") await new Promise(() => {});
    const doc: { verdict?: string; exit_code?: number; refusal?: string } = engine(body.command, body.args);
    return { json: async () => ({ version: "1", cli_line: "lockwire", exit_code: doc.exit_code ?? 0, verdict: doc.verdict ?? null, payload: doc.refusal ? null : doc, refusal: doc.refusal ?? null }) };
  },
}));
const { compareBatch, handleApply, NOT_REDONE, RECHECKED, recordBatch, recordProposal } = await import("./assistant");
const { COULD_NOT_COMPARE, compareProposal } = await import("./proposalCheck");

const C = join(dir, "lockwire.yaml");
const OVER = { limit: 15, draw: 32 };
const revision = () => createHash("sha256").update(readFileSync(C)).digest("hex");
/** Makes one card for `robot` and returns it. The person's file and folder must be exactly as they were: the stand-in writes the file it is told to, and a journal beside it. */
const card = async (robot: Robot, command: string, input: Record<string, unknown>) => {
  writeFileSync(C, JSON.stringify(robot));
  const made = command === "batch" ? recordBatch(C, revision(), [{ command: "declare", args: input }], "+ one", await compareBatch(C, [{ command: "declare", args: input }])) : ((await recordProposal(C, revision(), command, input)) as { proposal: ReturnType<typeof recordBatch> }).proposal;
  expect([readFileSync(C, "utf8"), readdirSync(dir)]).toEqual([JSON.stringify(robot), ["lockwire.yaml"]]);
  return made;
};
const servo = (kind: string, draw: number, limit: number, was?: string) => ({ kind, severity: "violation", quantity: "budgets/servo", observed: `${draw} W`, expected: `<= ${limit} W`, ...(was ? { was } : {}) });

describe("card shows before and after", () => {
  it.each([
    ["a declare that fixes a NO-GO flips the verdict and clears the finding", OVER, "declare", { path: "limit", value: 40, source: "s" }, { before: "NO-GO", after: "GO", changes: [servo("cleared", 32, 15)] }],
    ["an add that creates a violation lists it", { limit: 40, draw: 32 }, "add", { kind: "device", fields: { draw: 10 }, source: "s" }, { before: "GO", after: "NO-GO", changes: [servo("new", 42, 40)] }],
    ["a batch card carries it too, and a finding that only moves is one changed line", OVER, "batch", { path: "limit", value: 20, source: "s" }, { before: "NO-GO", after: "NO-GO", changes: [servo("changed", 32, 20, "32 W")] }],
    ["a swap, which takes no name, is compared too", OVER, "swap", { path: "limit", value: 40 }, { before: "NO-GO", after: "GO", changes: [servo("cleared", 32, 15)] }],
    ["a change with no effect: nothing changes", OVER, "declare", { path: "limit", value: 15, source: "s" }, { before: "NO-GO", after: "NO-GO", changes: [] }],
  ])("%s; the real lock file is byte-identical", async (_name, robot, command, input, comparison) => expect((await card(robot, command, input)).comparison).toEqual(comparison));

  it("a swap card runs the served swap-apply (no witness) on the copy: before and after, the finding cleared, written only on Apply", async () => {
    const made = await card(OVER, "swap", { joint: "wrist", part_id: "strong" });
    expect([made.command, made.comparison]).toEqual(["swap", { before: "NO-GO", after: "GO", changes: [servo("cleared", 32, 15)] }]);
    expect(await handleApply({ id: made.id, witness: "Ada" })).toEqual({ ok: true });
    expect(JSON.parse(readFileSync(C, "utf8"))).toEqual({ limit: 15, draw: 10 });
    rmSync(join(dir, ".lockwire.yaml.lockwire-undo.json")); // the signed write's journal: the next card wants a clean folder
  });

  it("the engine down: the card says it could not compare, never a verdict, and can still be signed", async () => {
    down = true;
    const made = await card(OVER, "declare", { path: "limit", value: 40, source: "s" });
    expect(made.comparison).toEqual({ error: COULD_NOT_COMPARE });
    expect(await handleApply({ id: made.id, witness: "Ada" })).toEqual({ ok: true });
    expect(JSON.parse(readFileSync(C, "utf8"))).toEqual({ limit: 40, draw: 32 });
  });
});

describe("a comparison that is refused, stuck or crashed leaves the folder clean", () => {
  afterEach(() => vi.useRealTimers());
  beforeEach(() => ((down = false), readdirSync(dir).filter((n) => n !== "lockwire.yaml").forEach((n) => rmSync(join(dir, n), { recursive: true, force: true })))); // the engine-down test leaves the engine down, the signing test leaves its journal
  const ID = (n: number) => `00000000-0000-4000-8000-00000000000${n}`;
  const files = () => readdirSync(dir).sort();

  it("a refused apply on the copy gives the fixed sentence, never a verdict", async () => {
    writeFileSync(C, JSON.stringify(OVER));
    expect(await compareProposal(C, async () => "refused")).toEqual({ error: COULD_NOT_COMPARE });
    expect(files()).toEqual(["lockwire.yaml"]);
  });

  it("past the bound the card returns the fixed sentence, the copy is gone, and a late engine reply is ignored", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    writeFileSync(C, JSON.stringify(OVER));
    let release!: () => void;
    let begun = false;
    const held = new Promise<void>((r) => (release = r));
    let copyPath = "";
    const pending = compareProposal(C, async (copy) => ((copyPath = copy), (begun = true), await held, writeFileSync(copy, "late"), null));
    while (!begun) await new Promise((r) => setImmediate(r)); // real turns of the loop; only setTimeout is faked
    expect(files()).toHaveLength(2); // the copy is there while the engine works
    let done = false;
    void pending.then(() => (done = true));
    await vi.advanceTimersByTimeAsync(14_999);
    expect(done).toBe(false); // the bound is 15 seconds, not less
    await vi.advanceTimersByTimeAsync(1);
    expect(await pending).toEqual({ error: COULD_NOT_COMPARE });
    expect(files()).toEqual(["lockwire.yaml"]);
    release(); // the engine answers late and writes its copy again
    await new Promise((r) => setImmediate(r));
    expect(existsSync(copyPath)).toBe(true); // the late write is left for the sweep...
    const old = new Date(Date.now() - 6 * 60_000);
    utimesSync(copyPath, old, old);
    vi.useRealTimers();
    await compareProposal(C, async () => null);
    expect(files()).toEqual(["lockwire.yaml"]); // ...and the next comparison clears it
  });

  it("the next comparison sweeps stale copies and journals, and only those", async () => {
    writeFileSync(C, JSON.stringify(OVER));
    const old = new Date(Date.now() - 6 * 60_000);
    const make = (name: string, stale: boolean) => (writeFileSync(join(dir, name), "x"), stale && utimesSync(join(dir, name), old, old));
    make(`.lockwire.yaml.card-${ID(1)}`, true);
    make(`..lockwire.yaml.card-${ID(1)}.lockwire-undo.json`, true);
    make(`.lockwire.yaml.card-${ID(2)}`, false); // another card in flight
    make(`..lockwire.yaml.card-${ID(2)}.lockwire-undo.json`, false);
    make(".lockwire.yaml.keep", true); // unrelated hidden file
    make(`.other.yaml.card-${ID(3)}`, true); // another lock's copy
    make(`.lockwire.yaml.card-${ID(4)}.bak`, true); // not one of the two shapes
    mkdirSync(join(dir, `.lockwire.yaml.card-${ID(5)}`));
    utimesSync(join(dir, `.lockwire.yaml.card-${ID(5)}`), old, old); // a folder
    writeFileSync(join(dir, "target"), "t");
    symlinkSync(join(dir, "target"), join(dir, `.lockwire.yaml.card-${ID(6)}`)); // a link, with a stale target
    utimesSync(join(dir, "target"), old, old);
    await compareProposal(C, async () => null);
    expect(files()).toEqual([`..lockwire.yaml.card-${ID(2)}.lockwire-undo.json`, `.lockwire.yaml.card-${ID(2)}`, `.lockwire.yaml.card-${ID(4)}.bak`, `.lockwire.yaml.card-${ID(5)}`, `.lockwire.yaml.card-${ID(6)}`, ".lockwire.yaml.keep", `.other.yaml.card-${ID(3)}`, "lockwire.yaml", "target"].sort());
  });
});

// The lock is unchanged but a model file beside it changed (another tool, the command line, the person): Apply works the card's
// Before and After out again, the same way, and writes nothing when they differ from what the person saw.
describe("re-check at signing", () => {
  afterEach(() => ((stall = false), vi.useRealTimers()));
  beforeEach(() => ((down = false), readdirSync(dir).filter((n) => n !== "lockwire.yaml").forEach((n) => rmSync(join(dir, n), { recursive: true, force: true }))));
  const FIX = { path: "limit", value: 40, source: "s" };
  const lock = () => JSON.parse(readFileSync(C, "utf8"));
  const moved = { before: "NO-GO", after: "NO-GO", changes: [servo("changed", 52, 40, "52 W")] }; // the model file adds 20 W: 40 W no longer clears it

  it("the re-show sentence is true both for a file edited outside and for an earlier card applied", () => {
    expect(RECHECKED).toBe("The robot changed since this card was made. Look at its Before and After again, then press Apply once more.");
  });

  it.each([["a one-step card", "declare"], ["a batch card (several steps hold the same rule)", "batch"]])("%s: the verdict after changes: nothing is written, the card gets the new comparison, a second Apply writes", async (_n, command) => {
    const made = await card(OVER, command, FIX);
    expect(made.comparison).toEqual({ before: "NO-GO", after: "GO", changes: [servo("cleared", 32, 15)] });
    writeFileSync(MODEL(), JSON.stringify({ extra: 20 }));
    expect(await handleApply({ id: made.id, witness: "Ada" })).toEqual({ ok: false, error: RECHECKED, comparison: moved });
    expect(lock()).toEqual(OVER); // nothing written
    expect(await handleApply({ id: made.id, witness: "Ada" })).toEqual({ ok: true }); // the second press matches what is on screen
    expect(lock()).toEqual({ limit: 40, draw: 32 });
  });

  it("two cards of one turn: once the first is applied, the second's Before and After are worked out on the file as it now is, and shown again before it writes", async () => {
    writeFileSync(C, JSON.stringify(OVER));
    const r = revision();
    const first = ((await recordProposal(C, r, "declare", FIX)) as { proposal: { id: string } }).proposal;
    const second = ((await recordProposal(C, r, "declare", { path: "limit", value: 20, source: "s" })) as { proposal: { id: string; comparison: unknown } }).proposal;
    expect(await handleApply({ id: first.id, witness: "Ada" })).toEqual({ ok: true });
    expect(await handleApply({ id: second.id, witness: "Ada" })).toEqual({ ok: false, error: RECHECKED, comparison: { before: "GO", after: "NO-GO", changes: [servo("new", 32, 20)] } });
    expect(lock()).toEqual({ limit: 40, draw: 32 });
    expect(await handleApply({ id: second.id, witness: "Ada" })).toEqual({ ok: true });
    expect(lock()).toEqual({ limit: 20, draw: 32 });
  });

  it("a model file change that leaves the comparison as shown applies on the first press", async () => {
    const made = await card(OVER, "declare", FIX);
    writeFileSync(MODEL(), JSON.stringify({ extra: 0 }));
    expect(await handleApply({ id: made.id, witness: "Ada" })).toEqual({ ok: true });
    expect(lock()).toEqual({ limit: 40, draw: 32 });
  });

  it.each(["declare", "batch"])("the lock itself changed (%s): the stale refusal still wins, no comparison is redone", async (command) => {
    const made = await card(OVER, command, FIX);
    writeFileSync(MODEL(), JSON.stringify({ extra: 20 }));
    writeFileSync(C, JSON.stringify({ ...OVER, draw: 33 }));
    const r = await handleApply({ id: made.id, witness: "Ada" });
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/changed/) });
    expect(r).not.toHaveProperty("comparison");
    expect(lock()).toEqual({ ...OVER, draw: 33 });
  });

  it("the comparison cannot be redone in time: nothing is written on the first press; the card says so, and the next press applies anyway", async () => {
    const made = await card(OVER, "declare", FIX);
    writeFileSync(MODEL(), JSON.stringify({ extra: 20 }));
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    stall = true;
    const pending = handleApply({ id: made.id, witness: "Ada" });
    await vi.advanceTimersByTimeAsync(15_000);
    expect(await pending).toEqual({ ok: false, error: NOT_REDONE, comparison: { error: COULD_NOT_COMPARE } });
    expect(lock()).toEqual(OVER);
    expect(await handleApply({ id: made.id, witness: "Ada" })).toEqual({ ok: true }); // still stalled: the card now shows no comparison, so it applies as shown
    expect(lock()).toEqual({ limit: 40, draw: 32 });
  });
});
