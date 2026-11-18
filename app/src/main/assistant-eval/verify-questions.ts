// Re-derives the harder exam's expected facts from the REAL engine, so the exam cannot rot when the engine changes. No key, no model.
// For each question it places the robot as the live eval does (robots.ts), runs the served commands in `engine` (the same
// `serve.dispatch` the app's sidecar runs, one Python process for all of them) and fails when:
//   a fact is not in what the model would see (the reply as JSON, cut where the assistant cuts a tool result), or a `doc` file;
//   a fact is not matched by any of the question's patterns (the facts and the scoring must not drift apart);
//   `verdict` is not the served check's verdict; a pattern does not compile; an `atLeast` name is not a tool row;
//   a `fixes` question has no what-if in `engine` that clears a violation and adds none;
//   a `datasheet` question's PDF does not give back every line of datasheet.ts to a real PDF reader (poppler's pdftotext; a note when it is not installed);
//   a `catalogOnly` question has no add in `engine` of a cited part a catalog-parts in `engine` returned;
//   a `cardAfter` question has no what-if in `engine` whose changed robot gets that verdict;
//   a `searched` question has no suggest-fixes in `engine` that returns a fix;
//   a `tools` pattern matches no tool row; the datasheet's WEIGHT is not one of its printed lines.
// Run by verify-questions.test.ts with the unit tests.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { DATASHEET_FILE, LINES, WEIGHT } from "./datasheet";
import { ROOT, robotPlacer } from "./robots";
import type { EvalQuestion } from "./score";

const MAX_RESULT_CHARS = 20_000; // assistant.ts cuts every tool result here: a fact past it is one the model never sees
type Reply = { verdict: string | null; refusal: string | null; payload: unknown };
type Body = { command: string; args: Record<string, unknown> };
const CATALOG = ["catalog-parts", "catalog-robots", "parts-match"]; // read the catalog, not a robot: they take no contract

// As serve.build_app does: plugins first (a Pro install adds its commands and page extras), then the command union.
const SERVE = `
import json, sys
from types import SimpleNamespace
from pydantic import TypeAdapter
from lockwire import plugins, serve
plugins.ensure()
state = SimpleNamespace(token="", robot=False)
for setup in plugins.SERVE_SETUP:
    setup(state)
T = TypeAdapter(serve.request_type())
replies = []
for body in json.load(sys.stdin):
    r = serve.dispatch(T.validate_python(body), state)
    replies.append({"verdict": r.verdict, "refusal": r.refusal, "payload": r.payload})
print(json.dumps({"served": sorted(serve.commands()), "replies": replies}))
`;

/** The served commands, run in one engine process; `served` is every command name the engine serves. */
export function serve(bodies: Body[]): { served: string[]; replies: Reply[] } {
  const out = execFileSync("uv", ["run", "--project", ROOT, "python", "-I", "-c", SERVE], { cwd: ROOT, input: JSON.stringify(bodies), encoding: "utf8", maxBuffer: 1 << 28 });
  return JSON.parse(out);
}

export async function verifyQuestions(questions: EvalQuestion[], toolNames: string[]): Promise<{ problems: string[]; notes: string[] }> {
  const tmp = mkdtempSync(join(tmpdir(), "verify-questions-"));
  try {
    const place = robotPlacer(tmp, async (urdf) => {
      const [r] = serve([{ command: "start", args: { urdf } }]).replies;
      if (!r || r.refusal) throw new Error(`start ${urdf}: ${r?.refusal}`);
      return (r.payload as { path: string }).path;
    });
    const asked: { q: EvalQuestion; bodies: Body[]; contract: string }[] = [];
    for (const q of questions) {
      const contract = await place(q);
      const revision = createHash("sha256").update(readFileSync(contract)).digest("hex"); // what-if names the bytes it compares, as the app does
      asked.push({ q, contract, bodies: (q.engine ?? []).map((e) => ({ command: e.command, args: { ...(CATALOG.includes(e.command) ? {} : { contract }), ...(e.command === "what-if" ? { expected_revision: revision } : {}), ...e.args } })) });
    }
    const { served, replies } = serve(asked.flatMap((a) => a.bodies));
    const problems: string[] = [], notes: string[] = [];
    let at = 0;
    for (const { q, bodies, contract } of asked) {
      const mine = replies.slice(at, (at += bodies.length));
      const bad = (why: string) => problems.push(`${q.id}: ${why}`);
      const patterns = [...(q.all ?? []), ...(q.none ?? []), ...(q.marked ?? []), ...(q.tools ?? []), ...(q.cardDiff ?? [])];
      for (const p of patterns) { try { new RegExp(p, "i"); } catch { bad(`pattern /${p}/ does not compile`); } }
      mine.forEach((r, i) => { if (r.refusal) bad(`${bodies[i]!.command} was refused: ${r.refusal}`); });
      const seen = [...mine.map((r) => JSON.stringify(r.payload).slice(0, MAX_RESULT_CHARS)), ...(q.doc ? [readFileSync(join(ROOT, q.doc), "utf8")] : [])].join("\n");
      const scoredBy = [...(q.all ?? []), ...(q.none ?? []), ...(q.cardDiff ?? [])].map((p) => new RegExp(p, "i"));
      for (const f of q.facts ?? []) {
        // JSON.stringify writes a quote inside a string as \" ; a fact is plain text, so look for it both ways
        if (!seen.includes(f) && !seen.includes(JSON.stringify(f).slice(1, -1))) bad(`fact "${f}" is not in ${[...bodies.map((b) => b.command), ...(q.doc ? [q.doc] : [])].join(" + ") || "nothing (no engine command or doc)"}`);
        if (!scoredBy.some((re) => re.test(f))) bad(`fact "${f}" is matched by none of the question's all/none/cardDiff patterns`);
      }
      if (q.verdict !== undefined) {
        const i = bodies.findIndex((b) => b.command === "check");
        if (i < 0) bad("verdict expected but no check in engine");
        else if (mine[i]!.verdict !== q.verdict) bad(`verdict ${q.verdict} expected, the engine says ${mine[i]!.verdict}`);
      }
      for (const n of q.atLeast?.of ?? []) if (!toolNames.includes(n)) bad(`"${n}" is not a tool the assistant has`);
      for (const p of q.tools ?? []) { try { if (!toolNames.some((n) => new RegExp(p, "i").test(n))) bad(`tools pattern /${p}/ matches no tool the assistant has`); } catch { /* reported above as not compiling */ } }
      if (q.fixes) {
        // A clean fix must exist, or the question asks for the impossible: some what-if in `engine` drops a violation and adds none.
        const violations = (doc: unknown) => ((doc as { findings?: { severity: string; quantity: string }[] } | undefined)?.findings ?? []).filter((f) => f.severity === "violation").map((f) => f.quantity);
        const clean = mine.filter((r, i) => bodies[i]!.command === "what-if" && r.payload).some((r) => {
          const p = r.payload as { current?: unknown; proposed?: unknown };
          const before = violations(p.current), after = violations(p.proposed);
          return after.length < before.length && after.every((v) => before.includes(v));
        });
        if (!clean) bad("no what-if in engine clears a violation without adding one, so no card can pass `fixes`");
      }
      if (q.datasheet) {
        if (!LINES.includes(`Weight ${WEIGHT}`)) bad(`the datasheet prints no "Weight ${WEIGHT}" line, the weight the scorer holds the card to`);
        let text: string | null = null;
        try { text = execFileSync("pdftotext", [join(dirname(contract), DATASHEET_FILE), "-"], { encoding: "utf8" }); } catch (e) { if ((e as { code?: string }).code !== "ENOENT") throw e; }
        if (text === null) notes.push(`${q.id}: pdftotext not installed; the datasheet's text was not read back`);
        else for (const l of LINES) if (!text.includes(l)) bad(`datasheet line "${l}" is not in the PDF's text`);
      }
      const of = (command: string) => mine.filter((r, i) => bodies[i]!.command === command && r.payload).map((r, i) => ({ payload: r.payload as Record<string, unknown>, args: bodies.filter((b) => b.command === command)[i]!.args }));
      if (q.catalogOnly) {
        const cited = new Set(of("catalog-parts").flatMap(({ payload }) => ((payload.parts as { id: string; label: string }[] | undefined) ?? []).filter((x) => x.label === "cited").map((x) => x.id)));
        if (!of("add").some(({ args }) => cited.has(args.id as string))) bad("no add in engine of a cited part that catalog-parts returned, so no card can pass `catalogOnly`");
      }
      if (q.cardAfter !== undefined) {
        const word = (v: unknown) => (typeof v === "string" ? v : (v as { verdict?: string } | undefined)?.verdict);
        if (!of("what-if").some(({ payload }) => word(payload.proposed) === q.cardAfter)) bad(`no what-if in engine makes the robot ${q.cardAfter}, so no card can pass \`cardAfter\``);
      }
      if (q.searched && !of("suggest-fixes").some(({ payload }) => Array.isArray(payload.fixes) && payload.fixes.length > 0)) bad("no suggest-fixes in engine returns a fix, so no card can pass `searched`");
      if (q.fixes && !served.includes("suggest-fixes")) notes.push(`${q.id}: suggest-fixes not served; the card's own engine comparison is what is scored`);
    }
    return { problems, notes };
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }
}
