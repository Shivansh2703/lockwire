// The Run face's payload shape — field-for-field against `lockwire_pro.health.HealthDocument`
// (`src/lockwire/health.py`, `.as_dict()`), the same document `lockwire health --json`
// writes and `lockwire.api.health_document` returns (one document, every
// door). Measured 2026-09-08 with
// `uv run python -c "import lockwire.api as A; print(A.health_document(...))"` against
// a scored copy of `tests/fixtures/contract_clean.yaml` and against
// `tests/fixtures/contract_empty.yaml` (day-one, empty ledger) — see `runFace.fixture.ts`.
//
// Like `checkFacePayload.ts`, this is hand-typed against measured output, not generated:
// `e1-d3`'s emitter does not carry this shape yet. Replace with the generated import the
// moment it does.

import type { HealthDocumentPayload } from "../../shared/pro/api.gen";

type VerdictWord = "REFUSED" | "NO-GO" | "GO_WITH_NOTES" | "GO" | "NOT_SCORED";

// Derived from the generated payload so a new state cannot be missed here.
type HealthState = HealthDocumentPayload["devices"][number]["state"];

/** One device's latest ledger row (`health.build_document`'s `devices` entries) — already
 * the LATEST row per device (`latest_by_device`, e3-h's reader); this face never re-walks
 * the ledger to find it. `word` and `reason` are the document's own words, rendered
 * verbatim — this face composes none of its own. */
export interface DeviceRow {
  id: string;
  /** Plain part name (engine's copy.human_name); `id` is the tooltip. */
  name?: string;
  kind: string;
  state: HealthState;
  word: string;
  run: string | null;
  reason: string;
  runs: number;
  /** `reason_code` "silent" = the device sent no samples at all. */
  coverage?: { reason_code: string };
}

export interface RunFacePayload {
  verdict: VerdictWord;
  exit_code: number;
  devices: DeviceRow[];
  not_watched: number;
  scored: number;
  /** True iff no applicable comparison exists anywhere in the
   * contract — branched on comparisons made, never on whether a baseline was authored.
   * When true, `devices` is empty and this face draws no chart furniture: no series, no
   * band, no table — only `next_step`, verbatim. */
  nothing_watched: boolean;
  /** The one sentence naming what to author, and the exact command that authors it —
   * `null` whenever `nothing_watched` is false. Never composed here: an invented
   * threshold or an implied verdict on an empty ledger is the one thing this face must
   * not do. */
  next_step: string | null;
  next_action?: string | null;
}
