// The history drawer's data model -- the shape `lockwire log --json` emits
// (`gitlog.as_json`, `src/lockwire/gitlog.py`: "the drawer's whole data model,
// one object per commit, no extra keys"). A generated twin exists
// (`LogEntry` and `LogPayload` in `../shared/api.gen`), but its `fact_diff` is typed as `DiffPayload`
// and these types are hand-kept here, field-for-field the same as `gitlog.LogEntry`/`factdiff.FactDiff` -- verified
// by running `lockwire log --json` against a real fixture repo (see history.fixture.ts).
// This duplicates the generated type: re-point this file at `../shared/api.gen` (same
// pattern `checkFacePayload.ts` used for `Card`/`Finding`) rather than keep the shape twice.

interface FactChange {
  path: string;
  old: unknown;
  new: unknown;
  witness: string | null;
  source: string | null;
  limit_raised: boolean;
}

interface EntityChange {
  kind: string;
  id: string;
}

interface FindingRef {
  severity: string;
  quantity: string;
}

interface FactDiff {
  changed: boolean;
  changes: FactChange[];
  added: EntityChange[];
  removed: EntityChange[];
  findings_new: FindingRef[];
  findings_gone: FindingRef[];
}

export interface LogEntry {
  sha: string;
  author: string;
  date: string;
  message: string;
  verdict: string;
  /** `null` only for the earliest commit touching this path -- there is no parent to diff against. */
  fact_diff: FactDiff | null;
}

/** A commit's first line as a sentence: no "docs:" prefix, first letter capital (mirrors copy.commit_summary). */
export function commitSummary(message: string): string {
  const line = message.trim().split("\n")[0]?.replace(/^\w+(\([^)]*\))?!?:\s*/, "") ?? "";
  return line.charAt(0).toUpperCase() + line.slice(1);
}
