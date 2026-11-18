// What a held change does to the engine's check, for its card: the verdict before and after, and the findings that
// appear, clear or change. The engine computes both sides; the person's file is only ever read.
// The change is applied to a hidden copy BESIDE the lockfile, not in the OS temp folder: measured 2026-11-15, the same
// bytes checked from another folder lose the model file next to the lock and report different findings.
// A failure of any step is "could not compare", never a verdict: the card can still be signed.
import { randomUUID } from "node:crypto";
import { constants, copyFileSync, lstatSync, readdirSync, rmSync } from "node:fs";
import { basename, dirname, join } from "node:path";

import { PRODUCT_NAME } from "../shared/branding";
import type { AssistantComparison, AssistantFindingChange } from "../shared/ipc-contract";
import { isRecord, runSidecarCommand } from "./ipcKit";

export const COULD_NOT_COMPARE = `${PRODUCT_NAME} could not compare before and after. You can still apply this change.`;
export const COMPARE_BOUND_MS = 15_000; // chosen: the slowest measured card took 2.1 s; past this the person is better served by "sign anyway"
const STALE_MS = 5 * 60_000; // chosen: far past the bound, so a card still in flight is never swept
const UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";
const MAX_TEXT = 240; // chosen: a finding's sentence fits; a robot file's long text does not flood the card

type Finding = Omit<AssistantFindingChange, "kind" | "was">;
const text = (v: unknown) => (typeof v === "string" ? v : "");
/** Findings are compared whole and cut only for the card. */
const clip = (c: AssistantFindingChange) => Object.fromEntries(Object.entries(c).map(([k, v]) => [k, v.slice(0, MAX_TEXT)])) as AssistantFindingChange;

/** The engine's check of one file: its verdict and findings, or null when there is no verdict to trust. */
async function check(contract: string): Promise<{ verdict: string; findings: Finding[] } | null> {
  const reply = await runSidecarCommand({ command: "check", args: { contract } });
  const doc = reply.payload;
  if (reply.refusal || !isRecord(doc) || typeof doc.verdict !== "string" || !Array.isArray(doc.findings)) return null;
  return { verdict: doc.verdict, findings: doc.findings.filter(isRecord).map((f) => ({ severity: text(f.severity), quantity: text(f.quantity), observed: text(f.observed), expected: text(f.expected) })) };
}

const same = (a: Finding, b: Finding) => a.quantity === b.quantity && a.severity === b.severity && a.observed === b.observed && a.expected === b.expected;
const rank = (f: Finding) => (f.severity === "violation" ? 0 : f.severity === "warning" ? 1 : 2);

/** Findings only in `after` are new, only in `before` cleared; one of each on the same quantity is one changed finding. Worst first.
 * ponytail: every finding is compared with every other, fine for the hundreds a robot has; index by quantity if a check ever returns thousands. */
export function findingChanges(before: Finding[], after: Finding[]): AssistantFindingChange[] {
  const gone = before.filter((b) => !after.some((a) => same(a, b)));
  const changes: AssistantFindingChange[] = after.filter((a) => !before.some((b) => same(a, b))).map((a) => {
    const i = gone.findIndex((b) => b.quantity === a.quantity);
    return i < 0 ? { kind: "new", ...a } : { kind: "changed", ...a, was: gone.splice(i, 1)[0]!.observed };
  });
  return [...changes, ...gone.map((b) => ({ kind: "cleared" as const, ...b }))].sort((a, b) => rank(a) - rank(b)).map(clip);
}

/** Removes what an earlier crashed comparison left beside `contract`: only its two exact name shapes (the copy and its undo journal),
 * only regular files (lstat: a link or folder is never touched), only when older than STALE_MS. */
function sweep(contract: string) {
  const folder = dirname(contract);
  const copy = `\\.${basename(contract).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\.card-${UUID}`;
  const mine = new RegExp(`^(${copy}|\\.${copy}\\.lockwire-undo\\.json)$`);
  try {
    for (const name of readdirSync(folder)) {
      if (!mine.test(name)) continue;
      const file = join(folder, name);
      const st = lstatSync(file);
      if (st.isFile() && Date.now() - st.mtimeMs > STALE_MS) rmSync(file, { force: true });
    }
  } catch {
    // the folder is gone or unreadable: nothing to sweep
  }
}

/** `apply` makes the held change on the copy it is given and returns the engine's refusal, or null. Never throws; never writes `contract`.
 * The whole comparison gets COMPARE_BOUND_MS; a reply that comes later is ignored. */
export async function compareProposal(contract: string, apply: (copy: string) => Promise<string | null>): Promise<AssistantComparison> {
  const failed = { error: COULD_NOT_COMPARE };
  const folder = dirname(contract);
  const token = randomUUID(); // names the copy and, through it, whatever the engine writes beside it (its undo journal)
  const copy = join(folder, `.${basename(contract)}.card-${token}`);
  sweep(contract);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let late = false;
  const run = async (): Promise<AssistantComparison> => {
    copyFileSync(contract, copy, constants.COPYFILE_EXCL); // first: a file that cannot be copied costs no engine call
    const before = await check(contract);
    if (late || !before || (await apply(copy)) !== null || late) return failed;
    const after = await check(copy);
    return after && !late ? { before: before.verdict, after: after.verdict, changes: findingChanges(before.findings, after.findings) } : failed;
  };
  try {
    return await Promise.race([run().catch(() => failed), new Promise<AssistantComparison>((resolve) => (timer = setTimeout(() => ((late = true), resolve(failed)), COMPARE_BOUND_MS)))]);
  } finally {
    clearTimeout(timer);
    try {
      for (const name of readdirSync(folder)) if (name.includes(token)) rmSync(join(folder, name), { force: true });
    } catch {
      // the folder is gone or unreadable: nothing of ours is left to remove
    }
  }
}
