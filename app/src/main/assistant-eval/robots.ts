// Where a harder-exam question's robot lives: a fresh copy in a temp folder, so no question inherits another's conversation or
// grounding memory (both are kept per contract path). Questions with the same `thread` share one copy, in file order.
// The live eval and verify-questions.ts both call this, so the engine facts are checked on the same files the model sees.
import { cpSync, existsSync, mkdtempSync, writeFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";

import { DATASHEET_FILE, datasheetPdf } from "./datasheet";
import type { EvalQuestion } from "./score";

export const ROOT = join(import.meta.dirname, "../../../..");
const EXAMPLES = join(ROOT, "src/lockwire/examples");

/** `start`: the engine's served `start` on a model file, returning the lockfile path it wrote (the app's path for a dropped model file). */
export function robotPlacer(tmp: string, start: (urdf: string) => Promise<string>) {
  const made = new Map<string, Promise<string>>();
  return (q: EvalQuestion): Promise<string> => {
    const key = q.thread ?? q.id;
    if (!made.has(key)) made.set(key, place(mkdtempSync(join(tmp, `${key}-`)), q, start));
    return made.get(key)!.then((contract) => {
      if (q.datasheet) writeFileSync(join(dirname(contract), DATASHEET_FILE), datasheetPdf()); // the exam's own test datasheet, beside the robot
      return contract;
    });
  };
}

async function place(dir: string, q: EvalQuestion, start: (urdf: string) => Promise<string>): Promise<string> {
  if (q.model) {
    const urdf = join(dir, basename(q.model));
    cpSync(join(ROOT, q.model), urdf);
    return start(urdf);
  }
  const folder = join(EXAMPLES, q.robot);
  if (existsSync(join(folder, "contract.yaml"))) { cpSync(folder, dir, { recursive: true }); return join(dir, "contract.yaml"); }
  cpSync(join(EXAMPLES, `${q.robot}.yaml`), join(dir, "lockwire.yaml")); // servo-bench ships as one file
  return join(dir, "lockwire.yaml");
}
