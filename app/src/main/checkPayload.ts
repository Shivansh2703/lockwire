// Builds the renderer's `CheckFacePayload` from the sidecar's real `check` reply
// (`CheckPayload`, `../shared/api.gen`, e1-d3's generator).
//
// Fills `robot`, `verdict`, `scored`, the three counts, the flat `findings`
// and (E2-hl) `headline` -- `doc.headline`, `api.check`'s own field
// (`report.verdict_headline`, shared with `gui.py`'s HTML page rather than
// re-derived here). `qualifier`/`cards` are
// the page payload's half of the join (see `../renderer/checkFacePayload.ts`); no sidecar
// command emits them together with a `check` reply, so they are left at their honest empty
// default here rather than computed; the renderer overlays the real page values.
import type { CheckPayload } from "../shared/api.gen";
import type { CheckFacePayload } from "../renderer/checkFacePayload";

export function checkPayloadToFace(doc: CheckPayload, cliLine: string): CheckFacePayload {
  return {
    robot: doc.robot,
    verdict: doc.verdict,
    headline: doc.headline,
    qualifier: "",
    scored: doc.scored,
    violations: doc.counts.violation,
    warnings: doc.counts.warning,
    unchecked: doc.counts.not_scored,
    findings: doc.findings,
    cards: [],
    cli_line: cliLine,
  };
}
