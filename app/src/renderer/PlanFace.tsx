import type { Finding, PlanPayload, RobotJoint } from "../shared/api.gen";
import { FindingList, findingItems, useSelection } from "./FindingList";
import { StatusPill, type Status } from "./kit";

const STEP: Record<string, [Status, string]> = { PASS: ["go", "Pass"], FAIL: ["bad", "Fail"], UNSCORED: ["muted", "Not scored"], ERROR: ["bad", "Error"] };

/** The step kind only ("plan/steps/0/rail_load/draw" -> "Rail load"), the same label everywhere. */
const humanise = (path: string) => {
  const text = (path.split("/")[3] ?? path).replace(/[_-]/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
};

/** Plan result: finding rows like Check's, then one compact row per plan step. */
export function PlanFace({
  payload, joints = [], selected, onSelect, onSelectFinding,
}: {
  payload: PlanPayload;
  joints?: RobotJoint[];
  selected?: number | null;
  onSelect?: (index: number | null) => void;
  onSelectFinding?: (finding: Finding) => void;
}) {
  const [sel, setSel] = useSelection(selected, onSelect);
  // A not-scored finding at a step path is the step's own row (with its reason); drop the finding row.
  // Items keep their engine index, so selection still speaks the unfiltered list.
  const stepPaths = new Set((payload.steps ?? []).map((st) => st.path));
  const dup = (f: Finding) => f.severity === "not_scored" && stepPaths.has(f.quantity);
  const items = findingItems(payload.findings, joints, onSelectFinding, payload.finding_labels).filter((it) => !dup(payload.findings[it.index!]!));
  // One row per step kind and status, unless a kept finding already names the step.
  const seen = new Set(payload.findings.filter((f) => !dup(f)).map((f) => f.quantity));
  const rows = (payload.steps ?? []).filter((step) => {
    const key = `${step.path.split("/").slice(0, 4).join("/")}/${step.status}`;
    if (seen.has(step.path) || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return (
    <>
      <FindingList items={items} selected={sel} onSelect={setSel} />
      {rows.map((step) => {
        const [status, word] = STEP[step.status] ?? ["muted", step.status];
        return (
          <div className="plan-outcome-row" data-status={step.status} key={step.path} title={step.path}>
            <span className="plan-outcome-path">{humanise(step.path)}</span>
            <StatusPill status={status}>{word}</StatusPill>
            {step.not_scored_reason && <span className="plan-outcome-reason">{step.not_scored_reason}</span>}
          </div>
        );
      })}
    </>
  );
}
