// The paid Assistant eval: every question in questions.json and questions-hard.json goes to the real model with the real engine. It SPENDS the key's money.
//   ANTHROPIC_API_KEY=... LOCKWIRE_EVAL_MODEL=<model id> npx vitest run -c vitest.eval.config.ts      (from app/)
// Keyless dry run, no spend: LOCKWIRE_EVAL_STUB=1 npx vitest run -c vitest.eval.config.ts   (stub.ts plays the model; only the local engine is reached).
// The whole run is ONE test, so vitest.eval.config.ts's testTimeout caps the whole run; each question's line is printed the moment it is scored.
// The first 18 run on the shipped examples as they always have. Each harder question runs on a fresh copy of its robot (robots.ts);
// the scorer also sees the tools the turn called (from the panel's own events), its cards, its verdict word and its ungrounded list,
// and the engine replies of catalog-parts and suggest-fixes (recorded where main calls the engine, ipcKit's runSidecarCommand; main is unchanged).
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { describe, expect, it, vi } from "vitest";

import type { AssistantProposal } from "../../shared/ipc-contract";

import hard from "./questions-hard.json";
import questions from "./questions.json";
import { robotPlacer } from "./robots";
import { installStub, SCRIPTED } from "./stub";
import { bySubject, clearsCleanly, countedCards, type EvalQuestion, type EvalReply, lacksComparison, score } from "./score";

const dir = mkdtempSync(join(tmpdir(), "assistant-eval-"));
const events: unknown[] = []; // what main sends the panel: the tool names of each turn
const panel = { isDestroyed: () => false, webContents: { send: (_channel: string, event: unknown) => events.push(event) } };
vi.mock("electron", () => ({
  app: { getPath: () => dir, isPackaged: false },
  BrowserWindow: { getAllWindows: () => [panel] },
  safeStorage: { isEncryptionAvailable: () => true, encryptString: (s: string) => Buffer.from(`enc:${s}`), decryptString: (b: Buffer) => b.toString().slice(4) },
}));
const replies: EvalReply[] = []; // every catalog-parts and suggest-fixes reply, whoever asked: the whole payload, before main's 20,000-character cut
vi.mock("../ipcKit", async (orig) => {
  const real = await orig<typeof import("../ipcKit")>();
  return { ...real, runSidecarCommand: async (request: unknown) => {
    const r = await real.runSidecarCommand(request), command = String((request as { command?: unknown }).command);
    if (["catalog-parts", "suggest-fixes"].includes(command) && !r.refusal) replies.push({ command, payload: r.payload });
    return r;
  } };
});
const { handleSend, handleSettingsSet } = await import("../assistant");
const { withShown } = await import("../assistantShow");
const { onSidecarState, startSidecar, stopSidecar } = await import("../sidecar");
const { runSidecarCommand } = await import("../ipcKit");

const stub = process.env.LOCKWIRE_EVAL_STUB === "1";
const key = stub ? "stub-not-a-key" : process.env.ANTHROPIC_API_KEY;
const model = stub ? "stub-model" : process.env.LOCKWIRE_EVAL_MODEL;

describe.skipIf(!key || !model)("Assistant eval (paid, or stubbed with LOCKWIRE_EVAL_STUB=1)", () => {
  it("answers the questions", async () => {
    // The first line printed says which run this is: a stub run never reads the key, and says so loudly when one is set.
    console.log(stub ? `STUB PROVIDER ON (LOCKWIRE_EVAL_STUB=1): no question goes to a model and nothing is spent${process.env.ANTHROPIC_API_KEY ? "; ANTHROPIC_API_KEY IS SET AND IS NOT USED" : ""}` : `PAID RUN: every question goes to ${model} on your key`);
    if (stub) installStub((line) => console.log(line));
    const ready = new Promise<void>((ok) => onSidecarState((s) => s.phase === "ready" && ok()));
    startSidecar({ command: "uv", args: ["run", "lockwire", "serve"], env: process.env });
    await ready;
    const place = robotPlacer(dir, async (urdf) => {
      const r = await runSidecarCommand({ command: "start", args: { urdf } });
      if (r.refusal) throw new Error(`start ${urdf}: ${r.refusal}`);
      return (r.payload as { path: string }).path;
    });
    const send = withShown(handleSend);
    const results: { q: EvalQuestion; pass: boolean }[] = [];
    const thread = new Map<string, AssistantProposal[]>();
    const told = new Map<string, EvalReply[]>(); // the engine replies of a thread so far (a question with no thread is its own)
    for (const q of [...(questions as EvalQuestion[]), ...(hard as EvalQuestion[])]) {
      const contract = q.subject ? await place(q) : resolve("../src/lockwire/examples", q.robot, "contract.yaml");
      await handleSettingsSet({ key, model, enableRobot: contract });
      const revision = createHash("sha256").update(readFileSync(contract)).digest("hex");
      events.length = 0;
      replies.length = 0;
      const turn = await send({ contract, revision, mode: "plan", text: q.question });
      const tools = events.flatMap((e) => ((e as { type?: string }).type === "tool" ? [(e as { name: string }).name] : []));
      // In a thread the cards so far count: the fix loop may offer the card on the "Why" turn, and "the smallest fix" then points back at it.
      const earlier = q.thread ? thread.get(q.thread) ?? [] : [];
      if (q.thread) thread.set(q.thread, [...earlier, ...turn.proposals]);
      const cards = countedCards(q, earlier, turn.proposals);
      const chat = q.thread ?? q.id;
      told.set(chat, [...(told.get(chat) ?? []), ...replies]);
      const r = score(q, {
        text: turn.text || turn.error || "", proposals: cards.length, tools, verdict: turn.verdict.word, ungrounded: turn.ungrounded,
        fixes: cards.filter((p) => clearsCleanly(p.comparison)).length, uncompared: cards.filter((p) => lacksComparison(p.comparison)).length,
        cards, replies: told.get(chat),
      });
      console.log(`${r.pass ? "PASS" : "FAIL"} ${q.subject ?? "lookup"} ${q.id}${r.pass ? "" : `: ${r.why.join("; ")}`}${turn.error ? ` [error: ${turn.error}]` : ""}`);
      for (const n of r.notes ?? []) console.log(`NOTE ${q.id}: ${n}`); // printed for a maintainer to read, never a failure
      results.push({ q, pass: r.pass });
    }
    stopSidecar();
    for (const line of bySubject(results)) console.log(stub ? `STUB ${line}` : line);
    const scripted = results.filter((r) => SCRIPTED.includes(r.q.id));
    console.log(stub
      ? `STUB RUN, NOT AN EXAM RESULT: ${scripted.filter((r) => r.pass).length}/${scripted.length} scripted questions passed; the other ${results.length - scripted.length} answered "stub answer"`
      : `${results.filter((r) => r.pass).length}/${results.length} passed`);
    // A stub run passes when every scripted question passes (the rest answer "stub answer" and fail on their text, as they must).
    expect(results.filter((r) => !r.pass && (!stub || SCRIPTED.includes(r.q.id))).map((r) => r.q.id)).toEqual([]);
  });
});
