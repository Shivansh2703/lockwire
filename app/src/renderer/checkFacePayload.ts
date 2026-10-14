// The Check face's payload shape.
//
// `Finding` and `Card` are the generated types (`../shared/api.gen`, emitted from
// `src/lockwire/api_payload.py`) -- re-exported here rather than hand-duplicated.
//
// `CheckFacePayload` itself has no generated twin: no door emits this exact union of fields.
// `lockwire.api.check` (the sidecar's `check` command, wrapped as `CheckPayload` in
// `../shared/api.gen`) emits `verdict`/`counts`/flat `findings` and `headline`;
// `main/checkPayload.ts`'s `checkPayloadToFace` fills those from the real reply and leaves the
// page-payload half (`qualifier`, grouped `cards`, `cli_line`) at its honest empty default
// (`""`, `[]`, `null`) rather than computing it, because computing it would mean re-implementing
// the page builder a second time. `App` overlays the real page values once it has them.
// Anything the optional pro/ folder shows on the Check page reads the page payload itself.

export type { Card } from "../shared/api.gen";
import type { Card, Finding } from "../shared/api.gen";

/** Also `CheckPayload["verdict"]` in `../shared/api.gen` -- spelled out again here
 * so this file does not have to import `CheckPayload`'s whole wrapped-document
 * shape just for one field. */
type VerdictWord = "REFUSED" | "NO-GO" | "GO_WITH_NOTES" | "GO" | "NOT_SCORED";

export interface CheckFacePayload {
  robot: string;
  verdict: VerdictWord;
  headline: string;
  qualifier: string;
  scored: number | null;
  violations: number;
  warnings: number;
  unchecked: number;
  findings: Finding[];
  cards: Card[];
  cli_line: string | null;
}
