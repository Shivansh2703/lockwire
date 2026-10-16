import type { Finding, PlanPayload, RobotJoint } from "../shared/api.gen";
import { FindingList, findingItems, useSelection } from "./FindingList";
import type { OnJump } from "./jump";
import { Button, StatusPill, type Status } from "./kit";

const STEP: Record<string, [Status, string]> = { PASS: ["go", "Pass"], FAIL: ["bad", "Fail"], UNSCORED: ["muted", "Not scored"], ERROR: ["bad", "Error"] };

/** The step kind only ("plan/steps/0/rail_load/draw" -> "Rail load"), the same label everywhere. */
const humanise = (path: string) => {
  const text = (path.split("/")[3] ?? path).replace(/[_-]/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
};

/** Plan result: finding rows like Check's, then one compact row per plan step. */
export function PlanFace({
  payload, joints = [], selected, onSelect, onSelectFinding, onJump,
}: {
  payload: PlanPayload;
  joints?: RobotJoint[];
  selected?: number | null;
  onSelect?: (index: number | null) => void;
  onSelectFinding?: (finding: Finding) => void;
  onJump?: OnJump;
}) {
  const [sel, setSel] = useSelection(selected, onSelect);
  // A not-scored finding at a step path is the step's own row (with its reason); drop the finding row.
  // Items keep their engine index, so selection still speaks the unfiltered list.
  const stepPaths = new Set((payload.steps ?? []).map((st) => st.path));
  const dup = (f: Finding) => f.severity === "not_scored" && stepPaths.has(f.quantity);
  const items = findingItems(payload.findings, joints, onSelectFinding, payload.finding_labels, onJump, true).filter((it) => !dup(payload.findings[it.index!]!));
  // One row per step kind and status, unless a kept finding already names the step.
  const seen = new Set(payload.findings.filter((f) => !dup(f)).map((f) => f.quantity));
  const rows = (payload.steps ?? []).filter((step) => {
    const key = `${step.path.split("/").slice(0, 4).join("/")}/${step.status}`;
    if (seen.has(step.path) || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  // Count what the rows show: one step per kind and status (two servos on one rail are one finding row).
  const kinds = new Set<string>();
  const steps = (payload.steps ?? []).filter((st) => { const k = `${st.path.split("/")[3]}/${st.status}`; return !kinds.has(k) && kinds.add(k); });
  const count = (status: string) => steps.filter((st) => st.status === status).length;
  const parts = [["pass", count("PASS")], ["failed", count("FAIL") + count("ERROR")], ["not scored", count("UNSCORED")]] as const;
  const total = steps.length === 0 ? null
    : `${steps.length} step${steps.length > 1 ? "s" : ""} · ${parts.every(([w, n]) => w === "pass" || !n) ? "all pass" : parts.filter(([, n]) => n).map(([w, n]) => `${n} ${w}`).join(", ")}`;
  return (
    <>
      <FindingList items={items} selected={sel} onSelect={setSel} />
      {rows.map((step) => {
        const [status, word] = STEP[step.status] ?? ["muted", step.status];
        const inner = (
          <>
            <span className="plan-outcome-path">{humanise(step.path)}</span>
            <StatusPill status={status}>{word}</StatusPill>
            {step.not_scored_reason && <span className="plan-outcome-reason">{step.not_scored_reason}</span>}
          </>
        );
        // A step row lights the parts it touches (or opens its quantity in Declare, when it is one).
        const row = onJump && step.targets?.length
          ? <button type="button" className="plan-outcome-row plan-outcome-btn" data-status={step.status} key={step.path} title={step.path} aria-label={`Show ${humanise(step.path)}`} onClick={() => onJump({ path: step.path, targets: step.targets ?? [] })}>{inner}</button>
          : <div className="plan-outcome-row" data-status={step.status} key={step.path} title={step.path}>{inner}</div>;
        // A gap is closed in Declare: one link from the reason to the page that fills it (after the catch).
        return step.status === "UNSCORED" && onJump
          ? <div className="plan-outcome-wrap" key={step.path}>{row}<Button variant="quiet" onClick={() => onJump({ declare: true, targets: step.targets ?? [] })}>Declare it</Button></div>
          : row;
      })}
      {total && <p className="plan-total">{total}</p>}
    </>
  );
}
