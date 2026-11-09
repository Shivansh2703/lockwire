import type { RobotJoint } from "../../shared/api.gen";
import type { PreflightPayload } from "../../shared/pro/types";
import { Disclosure, FindingRow } from "../kit";
import { FindingList, findingItems, useSelection } from "../FindingList";

/** Test result: the unpowered record-check's findings as Check-style rows. The verdict lives in
 * the panel header; this never reconstructs the producer's result. */
export function TestFace({
  payload, joints = [], selected, onSelect,
}: {
  payload: PreflightPayload;
  joints?: RobotJoint[];
  selected?: number | null;
  onSelect?: (index: number | null) => void;
}) {
  const [sel, setSel] = useSelection(selected, onSelect);
  // NO-GO with every row not-scored: the cause is the evidence the robot never gave.
  const missing = payload.verdict === "NO-GO" && !payload.findings.some((f) => f.severity === "violation") ? payload.missing_required_evidence ?? [] : [];
  const passed = payload.passed ?? [];
  return (
    <>
      {missing.length > 0 && <div className="check-list"><FindingRow severity="bad" title="Missing evidence" chips={missing.map((m) => m.replace(/-/g, " "))} /></div>}
      <FindingList items={findingItems(payload.findings, joints, undefined, payload.finding_labels)} selected={sel} onSelect={setSel} between={passed.length > 0 && (
        <Disclosure summary={`${passed.length} agree`}>
          {passed.map((p, i) => <FindingRow key={i} severity="go" title={p.label} note={`${p.observed} · ${p.expected}`} />)}
        </Disclosure>
      )} />
    </>
  );
}
