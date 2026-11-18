// Every tool of the assistant's table that reaches the engine, against the REAL engine's reply.
// tests/test_fixes.py runs each free one on a copy of a shipped robot with small valid arguments (the served dispatch) and writes
// engine-replies.gen.json: the tool's model input, the arguments sent, and the reply; a paid test writes the paid rows' twin under pro/. Here, main's own
// path builds the arguments from that input (toolArgs for an engine row, the row's own run for a local one) and must send exactly
// those; then the real reply goes through the app's real reply check. A tool with no row there fails, so a new tool needs one.
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

import { afterAll, describe, expect, it, vi } from "vitest";

import snapshot from "./assistant-args.gen.json";
import replies from "./engine-replies.gen.json";
import { parseSidecarRunReply, type SidecarRunReply } from "./ipcKit";

vi.mock("electron", () => ({ app: { getPath: () => tmpdir(), isPackaged: false }, BrowserWindow: { getAllWindows: () => [] }, safeStorage: {} }));
const proArgs = Object.values(import.meta.glob<Record<string, unknown>>("./pro/assistant-args.pro.gen.json", { eager: true, import: "default" }))[0] ?? {};
const hello = { commands: Object.entries({ ...snapshot, ...proArgs }).map(([name, args_schema]) => ({ name, args_schema })) }; // the engine's own argument schemas
// /v1/run answers a recorded call with its real reply (any other call: a refusal), so a local row goes through the app's real runSidecarCommand.
const sent: [string, Record<string, unknown>][] = [];
const OK = { version: "-", cli_line: "lockwire start", exit_code: 0, verdict: null, payload: null, refusal: null };
const NOT_RECORDED = { version: "-", cli_line: "-", exit_code: 2, verdict: null, payload: null, refusal: "not recorded" };
const answer = (body: { command: string; args: Record<string, unknown> }) => {
  sent.push([body.command, body.args]);
  if (body.command === "start") { copyFileSync(join(EXAMPLES, String(body.args.example), "contract.yaml"), String(body.args.out)); return OK; } // propose-robot reads a template's sizes: the engine's start, stood in
  const key = JSON.stringify(place([body.command, body.args]));
  return Object.values(table).find((e) => JSON.stringify([e.command, e.args]) === key)?.reply ?? NOT_RECORDED;
};
vi.mock("./sidecar", async (orig) => ({ ...(await orig<object>()), callSidecar: async (path: string, init?: { body?: string }) => ({ ok: true, json: async () => (path === "/v1/hello" ? hello : answer(JSON.parse(init!.body!))) }) }));
for (const load of Object.values(import.meta.glob("./pro/assistantProTools.ts"))) await load(); // Pro's rows join the table when Pro is here
const { loadTools, toolArgs } = await import("./assistant");
const { noteResult } = await import("./assistantDesign");
const { runSidecarCommand } = await import("./ipcKit");
const { TOOLS } = await import("./assistantTools");

type Entry = { command: string; input: Record<string, unknown>; args: Record<string, unknown>; reply: SidecarRunReply; /** Paths under ROBOT the row's own guard opens: laid out as placeholders. */ files?: string[] };
const proReplies = Object.values(import.meta.glob<Record<string, unknown>>("./pro/engine-replies.pro.gen.json", { eager: true, import: "default" }))[0] ?? {}; // the paid rows' twin, when Pro is here
const table = { ...replies, ...proReplies } as unknown as Record<string, Entry>;
// Local rows that never reach the engine. A new local row must be named here or have a row in the engine table.
const NO_ENGINE = ["list-guides", "read-guide", "what-can-you-do", "list-files", "search-files", "read-file", "show-all", "show-connections"];

// The robots' folders, laid out as the Python side lays them out, with the files the app's own guards open.
const T = realpathSync(mkdtempSync(join(tmpdir(), "engine-replies-")));
const FIXTURES = resolve(process.cwd(), "../tests/fixtures");
const EXAMPLES = resolve(process.cwd(), "../src/lockwire/examples");
mkdirSync(join(T, "arm")); mkdirSync(join(T, "bench")); mkdirSync(join(T, "rover"));
copyFileSync(join(EXAMPLES, "arm-power/contract.yaml"), join(T, "arm/contract.yaml")); // the design tools copy and read the real robot
copyFileSync(join(EXAMPLES, "rover-param/contract.yaml"), join(T, "rover/contract.yaml"));
writeFileSync(join(T, "arm/sheet.pdf"), "%PDF-1.4\n");
for (const f of Object.values(table).flatMap((e) => e.files ?? [])) { mkdirSync(dirname(join(T, f)), { recursive: true }); writeFileSync(join(T, f), "{}\n"); }
cpSync(join(FIXTURES, "so101"), join(T, "so101"), { recursive: true });
afterAll(() => rmSync(T, { recursive: true, force: true }));
const place = (v: unknown) => JSON.parse(JSON.stringify(v).replaceAll(T, "ROBOT"));
const contractOf = (e?: Entry) => (typeof e?.args.contract === "string" ? e.args.contract.replace("ROBOT", T) : join(T, "arm/contract.yaml"));

await loadTools();

describe("every assistant tool against the real engine's reply", () => {
  const proHere = Object.keys(proArgs).length > 0; // the free tree has no Pro rows: the table's Pro rows are not its own
  it.skipIf(!proHere)("the engine table names only tools the table has", () => expect(Object.keys(table).filter((n) => !TOOLS.some((t) => t.name === n))).toEqual([]));

  it.each(TOOLS.map((t) => [t.name, t] as const))("%s: main sends the arguments the engine was run with, and the real reply passes the app's check", async (name, row) => {
    const e = table[name];
    if (row.pro && !hello.commands.some((c) => c.name === (row.command ?? row.name))) return; // free: the loop answers "part of Pro", nothing reaches the engine
    if (row.runs === "engine") {
      expect(e, `${name} reaches the engine and has no row in tests/test_fixes.py's engine table`).toBeDefined();
      expect(row.command ?? row.name).toBe(e!.command);
      expect(place(toolArgs(name, e!.input, contractOf(e), "REV"))).toEqual(e!.args);
    } else {
      sent.length = 0;
      const contract = name === "propose-robot" ? join(T, "rover/contract.yaml") : contractOf(e);
      if (name === "propose-power-tree") noteResult(contract, "catalog-parts", {}, { parts: [{ ...(e!.input.trees as { pack: object }[])[0]!.pack, label: "cited" }] }, { pdf: 0 }); // as if catalog-parts returned it this conversation
      const turn = { pdf: 0, read: new Set([join(T, "arm/sheet.pdf")]) }; // propose-part: read-file sent that datasheet this turn
      await row.run!(e?.input ?? {}, { contract, revision: "REV", engine: (command: string, args: Record<string, unknown>) => runSidecarCommand({ command, args }), served: new Set(hello.commands.map((c) => c.name)), turn } as never);
      if (!e) return expect(NO_ENGINE.includes(name) && sent.length === 0, `${name}: a local row that is not in NO_ENGINE needs a row in the engine table`).toBe(true);
      expect(place(sent), `${name} sends the recorded call`).toContainEqual([e.command, e.args]);
    }
    const reply = parseSidecarRunReply(e!.reply); // throws "sidecar: invalid run reply" on a shape the real app refuses
    if (row.kind === "propose") expect(reply.refusal, `${name}: the dry run is a card`).toBeNull();
    else expect(reply.refusal === null || !/refusing to write|invalid run reply/.test(reply.refusal), `${name}: content or a plain refusal, never a write-guard error for a read`).toBe(true);
  });

  it("a payload that names a verdict with no exit code is refused (the shape suggest-fixes had)", () => {
    const old = { ...table["suggest-fixes"]!.reply, payload: { ...(table["suggest-fixes"]!.reply.payload as object), verdict: "NO-GO" } };
    expect(() => parseSidecarRunReply(old)).toThrow("sidecar: invalid run reply");
  });
});
