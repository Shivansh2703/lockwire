import { useState } from "react";

import type { Finding, PlanPayload, RobotJoint } from "../shared/api.gen";
import { verdictPill } from "./FindingList";
import { Button, EmptyState, IconButton, Notice, Panel } from "./kit";
import type { OnJump } from "./jump";
import { PlanFace } from "./PlanFace";

export interface PlanInputProps {
  /** The router supplies the real file-picker and served-plan request. */
  onChoosePlan: () => Promise<{ path: string; payload: PlanPayload } | null>;
  payload: PlanPayload | null;
  /** Chip text for a plan the app loaded itself from beside the lockfile. */
  autoName?: string;
  /** The robot's display name: a chosen plan named after it is titled with it. */
  robotName?: string;
  /** Why that plan could not be read, when one sat beside the lockfile. */
  autoError?: string | null;
  onPlan: (payload: PlanPayload | null) => void;
  onSelectFinding?: (finding: Finding) => void;
  joints?: RobotJoint[];
  selected?: number | null;
  onSelect?: (index: number | null) => void;
  onJump?: OnJump;
}

const baseName = (path: string) => path.split(/[\\/]/).pop() || path;
/** "plan-x500.yaml" -> "x500 plan": the chip speaks plainly, the file name is its tooltip. */
export const planName = (path: string, robot?: string) => {
  const stem = baseName(path).replace(/\.[^.]*$/, "").replace(/^plan([-_]|$)|([-_]|^)plan$/i, "").replace(/[-_]/g, " ").trim();
  // The robot's display name ("ANYmal") wins over the file's lowercase stem ("anymal").
  return robot && robot.replace(/\W/g, "").toLowerCase() === stem.replace(/\W/g, "").toLowerCase() ? `${robot} plan` : stem ? `${stem} plan` : "Plan";
};

/** One right-hand Panel: pick a plan, see the verdict and its findings. The producer owns every
 * displayed result; the router keeps it across tab changes. */
export function PlanInput({ onChoosePlan, payload, autoName, robotName, autoError, onPlan, onSelectFinding, joints, selected, onSelect, onJump }: PlanInputProps) {
  const [error, setError] = useState<string | null>(null);
  const [plan, setPlan] = useState<string | null>(null);

  async function choosePlan() {
    setError(null);
    try {
      const next = await onChoosePlan();
      if (!next) return;
      setPlan(next.path);
      onSelect?.(null);
      onPlan(next.payload);
    } catch (error) {
      setError(error instanceof Error ? error.message : String(error));
    }
  }

  const unscored = payload?.steps?.filter((st) => st.status === "UNSCORED").length ?? 0;
  const firstProblem = payload?.findings.findIndex((f) => f.severity === "violation" || f.severity === "warning") ?? -1;
  return (
    <Panel title="Plan" actions={payload && <span className="verdict-word" role="status">{firstProblem >= 0 && onSelect && selected !== firstProblem
      ? <button type="button" className="kit-pill-btn" aria-label="Show the first problem" onClick={() => onSelect(firstProblem)}>{verdictPill(payload.verdict)}</button>
      : verdictPill(payload.verdict)}{payload.verdict === "GO" && unscored > 0 && <small className="plan-unscored"> {unscored} step{unscored > 1 ? "s" : ""} not scored</small>}</span>}>
      <div className="plan-input">
        <span className="kit-filepick">
        <Button variant="quiet" onClick={choosePlan}>Choose plan</Button>
        {(plan || (payload && autoName)) && (
          <span className="kit-cli" title={plan ? baseName(plan) : autoName}>
            <span className="kit-cli-text">{plan ? planName(plan, robotName) : autoName}</span>
            <IconButton icon="close" label="Remove file" onClick={() => { setPlan(null); onSelect?.(null); onPlan(null); }} />
          </span>
        )}
        </span>
      </div>
      {!plan && !payload && !error && <EmptyState title="No plan chosen" />}
      {autoError && !payload && !plan && <p className="plan-total">The example plan couldn't be read: {autoError}</p>}
      {error && <Notice kind="error">Plan failed: {error}</Notice>}
      {payload && <PlanFace payload={payload} joints={joints} selected={selected} onSelect={onSelect} onSelectFinding={onSelectFinding} onJump={onJump} />}
    </Panel>
  );
}
