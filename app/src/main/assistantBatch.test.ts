// One card of several edits (recordBatch, then a person's Apply): all or nothing, one engine batch-edit, one undo step; and the catalog and
// template helpers the design tools stand on. The sidecar is the REAL engine (one python call per command). Moved here from the old Agent
// tab's robot test when that tab was removed: these rules guard the assistant's store and its design tools.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { appendFileSync, copyFileSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { beforeEach, describe, expect, it, vi } from "vitest";

const dir = mkdtempSync(join(tmpdir(), "assistant-batch-"));
vi.mock("electron", () => ({ app: { getPath: () => dir, isPackaged: false }, BrowserWindow: { getAllWindows: () => [] }, safeStorage: {} }));
const ROOT = resolve(process.cwd(), ".."); // vitest runs from app/
const py = (code: string, input = "") => execFileSync("uv", ["run", "--project", ROOT, "python", "-I", "-c", code], { input, encoding: "utf8", cwd: ROOT, maxBuffer: 1 << 26 });
const RUN = "import sys,json\nfrom pydantic import TypeAdapter\nfrom lockwire.serve import dispatch, request_type\nprint(dispatch(TypeAdapter(request_type()).validate_python(json.load(sys.stdin))).model_dump_json())";
const callSidecar = vi.fn(async (_path: string, init?: { body?: string }) => ({ json: async () => JSON.parse(py(RUN, init!.body!)) }));
vi.mock("./sidecar", async (orig) => ({ ...(await orig<object>()), callSidecar: (...a: [string, { body?: string }]) => callSidecar(...a) }));

const { handleApply, recordBatch, runSteps } = await import("./assistant");
const { citedParts, templateDims, templateUnreadable, UNCLASSIFIED_FAMILY } = await import("./assistantDesign");

const robot = mkdtempSync(join(tmpdir(), "robot-rover-"));
const contract = join(robot, "lockwire.yaml");
const TEMPLATE = join(ROOT, "src/lockwire/examples/rover-param/contract.yaml");
beforeEach(() => copyFileSync(TEMPLATE, contract)); // each test starts from the untouched template
const bytes = () => readFileSync(contract, "utf8");
const revision = (file = contract) => createHash("sha256").update(readFileSync(file)).digest("hex");
const PACK = "pack.gensace.3s-5000-60c-short";
const JOURNAL = join(robot, ".lockwire.yaml.lockwire-undo.json"); // the person's own undo journal (UNDO-1)
const engine = (command: string, args: object) => JSON.parse(py(RUN, JSON.stringify({ command, args }))) as { refusal?: string | null };
const folder = () => Object.fromEntries(readdirSync(robot).map((f) => [f, readFileSync(join(robot, f), "hex")]));

describe("a card of several edits", () => {
  it("a signed batch whose step 3 of 4 fails writes nothing: the folder (the person's own undo journal included) is byte-identical and one sentence says so", async () => {
    writeFileSync(JOURNAL, '{"undo": [], "redo": [], "mine": true}');
    const before = folder();
    const size = (name: string, value: number) => ({ command: "declare", args: { path: `params/${name}/value`, value, source: "your description (a size the assistant chose)" } });
    const add = { command: "add", args: { kind: "device", id: PACK, source: "Lockwire parts library", fields: { family: "fam.unclassified", model: "m", owner: "robot" } } };
    const card = recordBatch(contract, revision(), [size("chassis_length", 0.42), add, add, size("chassis_width", 0.3)], "+ four changes");
    const r = await handleApply({ id: card.id, witness: "Test Person" });
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/^Nothing was written: /) });
    expect(folder()).toEqual(before); // no copy, no copy's journal, every file's bytes unchanged
  }, 180_000);

  const sizeStep = (name: string, value: number) => ({ command: "declare", args: { path: `params/${name}/value`, value, source: "your description (a size the assistant chose)" } });
  const sizeCard = () => recordBatch(contract, revision(), [sizeStep("chassis_length", 0.42)], "+ one change");
  const mixedSteps = [sizeStep("chassis_length", 0.42), { command: "add", args: { kind: "device", id: PACK, source: "Lockwire parts library", fields: { family: "fam.unclassified", model: "m", owner: "robot" } } }];
  const mixedCard = () => recordBatch(contract, revision(), mixedSteps, "+ two changes");
  it("a batch signed after the robot file changed writes nothing, runs no step, and the person's edit stays", async () => {
    const card = sizeCard();
    appendFileSync(contract, "# edited by hand\n");
    const edited = bytes();
    callSidecar.mockClear();
    expect(await handleApply({ id: card.id, witness: "Test Person" })).toMatchObject({ ok: false, error: expect.stringMatching(/^Nothing was written: the robot changed/) });
    expect(bytes()).toBe(edited);
    expect(callSidecar).not.toHaveBeenCalled(); // refused before any engine run on a stale proposal
  }, 180_000);

  const sent = () => callSidecar.mock.calls.filter(([p]) => p === "/v1/run").map(([, i]) => JSON.parse(i!.body!) as { command: string; args: { journal?: boolean } });
  it("a mixed (declare + add) batch is ONE engine batch-edit on the person's file (journal on), and undo restores the file byte-identically", async () => {
    const before = bytes();
    const card = mixedCard();
    callSidecar.mockClear();
    expect(await handleApply({ id: card.id, witness: "Test Person" })).toEqual({ ok: true });
    expect(sent().map((c) => [c.command, c.args.journal])).toEqual([["batch-edit", true]]); // not a step per change
    expect(bytes()).not.toBe(before);
    expect(engine("undo-edit", { contract }).refusal).toBeFalsy();
    expect(bytes()).toBe(before); // one accept is one undo step
  }, 180_000);

  it("a declare-only batch the engine refuses writes nothing: the folder is byte-identical", async () => {
    writeFileSync(JOURNAL, '{"undo": [], "redo": [], "mine": true}');
    const before = folder();
    const card = recordBatch(contract, revision(), [sizeStep("chassis_length", 0.42), sizeStep("no_such_size", 1)], "+ two changes");
    expect(await handleApply({ id: card.id, witness: "Test Person" })).toMatchObject({ ok: false, error: expect.stringMatching(/^Nothing was written: /) });
    expect(folder()).toEqual(before);
  }, 180_000);

  it("a step the engine has no op for is refused before any engine run, on the person's file and on a copy", async () => {
    const before = folder();
    const card = recordBatch(contract, revision(), [sizeStep("chassis_length", 0.42), { command: "frobnicate", args: {} }], "+ two changes");
    callSidecar.mockClear();
    expect(await handleApply({ id: card.id, witness: "Test Person" })).toMatchObject({ ok: false, error: expect.stringMatching(/^Nothing was written: unknown step/) });
    expect(await runSteps(contract, [{ command: "frobnicate", args: {} }], "Test Person")).toMatch(/^unknown step/);
    expect([callSidecar.mock.calls.length, folder()]).toEqual([0, before]);
  }, 180_000);

  it("a mixed batch on a hidden working copy leaves no journal beside it", async () => {
    const hidden = mkdtempSync(join(tmpdir(), "robot-hidden-"));
    copyFileSync(TEMPLATE, join(hidden, "lockwire.yaml"));
    expect(await runSteps(join(hidden, "lockwire.yaml"), mixedSteps, "Test Person")).toBeNull();
    expect(sent().at(-1)!.args.journal).toBe(false);
    expect(readdirSync(hidden)).toEqual(["lockwire.yaml"]);
  }, 180_000);

  it("a mixed batch signed after the file changed under the engine is refused by the engine's revision guard", async () => {
    const card = mixedCard();
    const real = callSidecar.getMockImplementation()!;
    callSidecar.mockImplementationOnce(async (path, init) => { appendFileSync(contract, "# edited mid-run\n"); return real(path, init); });
    const r = await handleApply({ id: card.id, witness: "Test Person" }).finally(() => callSidecar.mockImplementation(real));
    expect(r).toMatchObject({ ok: false, error: expect.stringMatching(/^Nothing was written: /) });
    expect(bytes()).toBe(`${readFileSync(TEMPLATE, "utf8")}# edited mid-run\n`);
  }, 180_000);

});

describe("the catalog and template helpers the design tools stand on", () => {
  const leftovers = () => readdirSync(dir).filter((f) => /^lockwire-(candidate|template)-/.test(f));
  it("a part counts only as the catalog returned it, labelled cited; its family is neutral and its source names the library row, never the model's", () => {
    const seen = new Map([["dynamixel.xl430-w250", { name: "XL430", label: "cited" }], ["cell.b", { name: "Cell B", label: "unverified" }]]);
    expect(citedParts([{ id: "cell.b", name: "Cell B" }], seen, "power trees", false, "catalog-parts")).toHaveProperty("refusal"); // unverified
    expect(citedParts([{ id: "dynamixel.xl430-w250", name: "renamed" }], seen, "power trees", false, "catalog-parts")).toHaveProperty("refusal"); // renamed
    expect(citedParts([{ id: "fict.warp", name: "Warp" }], seen, "power trees", false, "catalog-parts")).toHaveProperty("refusal"); // never returned
    const ok = citedParts([{ id: "dynamixel.xl430-w250", name: "XL430" }], seen, "power trees", false, "catalog-parts") as { parts: { family: string; source: string }[] };
    expect(ok.parts).toEqual([expect.objectContaining({ family: UNCLASSIFIED_FAMILY, source: expect.stringContaining("parts library: dynamixel.xl430-w250") })]); // no maker word a keyword check could read
  });

  it("the size names and ranges are read at runtime from each template's own params, exactly as the engine loads them; an unknown template allows none", async () => {
    const engineParams = (t: string) => JSON.parse(py(`import json\nfrom pathlib import Path\nfrom lockwire.schema import load_contract\nprint(json.dumps({p.name: [p.min, p.max] for p in load_contract(Path("src/lockwire/examples/${t}/contract.yaml")).params or []}))`));
    expect(await templateDims("rover-param")).toEqual(engineParams("rover-param"));
    expect(await templateDims("hexapod-param")).toEqual(engineParams("hexapod-param"));
    expect(await templateDims("no-such-template")).toEqual({}); // the real engine's refusal: no example by that name
    expect(leftovers()).toEqual([]);
  }, 60_000);

  it("an engine that fails while reading a template refuses in one plain sentence and is tried again next time", async () => {
    const real = callSidecar.getMockImplementation()!;
    callSidecar.mockImplementationOnce(async () => { throw new Error("ECONNRESET 127.0.0.1:53111"); });
    await expect(templateDims("servo-bench")).rejects.toThrow(templateUnreadable("servo-bench"));
    callSidecar.mockClear();
    await templateDims("servo-bench"); // the failure was not cached
    expect(callSidecar).toHaveBeenCalledTimes(1);
    callSidecar.mockImplementation(real);
    expect(leftovers()).toEqual([]);
  }, 60_000);
});
