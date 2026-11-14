// Save a Copy against the real engine: the copy must check exactly like the original.
import { execFileSync, spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

const ROOT = resolve(__dirname, "../../..");
const FIX = join(ROOT, "tests/fixtures");
const LOCKWIRE = join(ROOT, ".venv/bin/lockwire");
const pick = vi.fn();
const showMessageBox = vi.fn();
vi.mock("./dialogs", () => ({ pickSaveTarget: (...a: unknown[]) => pick(...a) }));
const door = { port: 0, token: "" };
vi.mock("./sidecar", () => ({ resolveSidecarCommand: () => ({ command: LOCKWIRE, args: [] }), scrubChildEnv: (e: object) => e, callSidecar: (path: string, init: RequestInit) => fetch(`http://127.0.0.1:${door.port}${path}`, { ...init, headers: { ...init.headers, "X-Lockwire-Token": door.token } }) }));
vi.mock("electron", () => ({ dialog: { showMessageBox: (o: unknown) => showMessageBox(o) }, shell: { showItemInFolder: vi.fn() } }));

import { saveCopy } from "./exports";

const LOGIN = "zz-login-probe";
let engine: ChildProcessWithoutNullStreams;
let dir = "";
let own = "";
let bare = "";
const env = { USER: process.env.USER, LOGNAME: process.env.LOGNAME };
// The verdict: exit code plus each finding's status and check name (the text names the file, which differs by folder).
const check = (file: string) => { try { return { out: execFileSync(LOCKWIRE, ["check", file, "--format", "csv"], { encoding: "utf8" }), code: 0 }; } catch (e) { const r = e as { stdout: string; status: number }; return { out: r.stdout, code: r.status }; } };
const verdict = (file: string) => { const r = check(file); return [r.code, ...r.out.split("\n").filter((l) => /^(violation|not_scored|warning|pass)/.test(l)).map((l) => l.split(",").slice(0, 2).join(","))]; };
const bytes = (f: string) => readFileSync(f);
// the real engine, as the app runs it, on a computer whose login is LOGIN and which has no saved name
beforeAll(async () => {
  Object.assign(process.env, { USER: LOGIN, LOGNAME: LOGIN });
  const home = mkdtempSync(join(tmpdir(), "copy-home-"));
  engine = spawn(LOCKWIRE, ["serve", "--exit-on-stdin-close"], { env: { PATH: process.env.PATH, HOME: home, USER: LOGIN, LOGNAME: LOGIN, LOCKWIRE_WITNESS_CONFIG: join(home, "none.toml"), LOCKWIRE_LOG_DIR: home } });
  const ready = JSON.parse(await new Promise<string>((ok) => createInterface({ input: engine.stdout }).once("line", ok)));
  door.port = ready.port;
  door.token = ready.token;
}, 60_000);
afterAll(() => { engine.kill(); Object.assign(process.env, env); });
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "copy-"));
  vi.clearAllMocks();
  mkdirSync(join(dir, "orig"));
  mkdirSync(join(dir, "elsewhere"));
  own = join(dir, "orig", "contract_clean.yaml");
  copyFileSync(join(FIX, "contract_clean.yaml"), own);
  copyFileSync(join(FIX, "anymal_d.urdf"), join(dir, "orig", "testbot.urdf"));
  // the same robot with nobody on record for its model (a 0.7-only shape)
  bare = join(dir, "orig", "bare.yaml");
  writeFileSync(bare, readFileSync(own, "utf8").replace('schema_version: "0.3"', 'schema_version: "0.7"').replace("    witness: w0\n    source: testbot.urdf\n", "    source: testbot.urdf\n"));
});
afterEach(() => rmSync(dir, { recursive: true, force: true }));

describe("Save a copy keeps the model reachable", { timeout: 90_000 }, () => {
  it("a relative model path saved to another folder checks exactly like the original", async () => {
    const copy = join(dir, "elsewhere", "copy.yaml");
    pick.mockResolvedValue(copy);
    await saveCopy(own);
    expect(readFileSync(copy, "utf8")).toContain("source: ../orig/testbot.urdf");
    expect(verdict(copy)).toEqual(verdict(own));
    expect(verdict(own)).toContain("violation,CAD file linked");
  });

  it("a witnessed model keeps its witness and the original's format", async () => {
    const copy = join(dir, "elsewhere", "copy.yaml");
    pick.mockResolvedValue(copy);
    await saveCopy(own);
    expect(readFileSync(copy, "utf8")).toContain('schema_version: "0.3"\n');
    expect(readFileSync(copy, "utf8")).toMatch(/provenance:\n {4}witness: w0\n {4}source: testbot\.urdf; source from Save a Copy/);
    expect(readFileSync(copy, "utf8")).not.toContain(LOGIN);
  });

  it("with nobody on record and no saved name the copy records no name and never the computer login", async () => {
    const copy = join(dir, "elsewhere", "copy.yaml");
    pick.mockResolvedValue(copy);
    await saveCopy(bare);
    expect(readFileSync(copy, "utf8")).toContain("source: ../orig/testbot.urdf");
    expect(readFileSync(copy, "utf8")).not.toContain(LOGIN);
  });

  it("with nobody on record the copy is 0.7 and says so in plain words", async () => {
    const copy = join(dir, "elsewhere", "copy.yaml");
    pick.mockResolvedValue(copy);
    await saveCopy(bare);
    expect(readFileSync(copy, "utf8")).toContain('schema_version: "0.7"\n');
    expect(showMessageBox.mock.calls[0]![0].detail).toBe("The model path was updated so the copy still finds its model. The copy is saved in the newer 0.7 format, which Lockwire 0.2.8 and older cannot open.");
  });

  it("says in plain words that the model path was updated", async () => {
    pick.mockResolvedValue(join(dir, "elsewhere", "copy.yaml"));
    await saveCopy(own);
    expect(showMessageBox.mock.calls[0]![0].detail).toBe("The model path was updated so the copy still finds its model.");
  });

  it("a copy in the same folder stays byte-identical and says nothing", async () => {
    pick.mockResolvedValue(join(dir, "orig", "copy.yaml"));
    await saveCopy(own);
    expect(bytes(join(dir, "orig", "copy.yaml")).equals(bytes(own))).toBe(true);
    expect(showMessageBox).not.toHaveBeenCalled();
  });

  it("an absolute model path saved to another folder stays byte-identical", async () => {
    const abs = join(dir, "orig", "abs.yaml");
    writeFileSync(abs, readFileSync(own, "utf8").replace("source: testbot.urdf\n", `source: ${join(dir, "orig", "testbot.urdf")}\n`));
    pick.mockResolvedValue(join(dir, "elsewhere", "copy.yaml"));
    await saveCopy(abs);
    expect(bytes(join(dir, "elsewhere", "copy.yaml")).equals(bytes(abs))).toBe(true);
    expect(showMessageBox).not.toHaveBeenCalled();
  });
});
