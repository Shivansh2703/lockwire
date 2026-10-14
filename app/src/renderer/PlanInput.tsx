import { useState } from "react";

import type { Finding, PlanPayload, RobotJoint } from "../shared/api.gen";
import { verdictPill } from "./FindingList";
import { EmptyState, FilePick, Notice, Panel } from "./kit";
import { PlanFace } from "./PlanFace";

export interface PlanInputProps {
  /** The router supplies the real file-picker and served-plan request. */
  onChoosePlan: () => Promise<{ path: string; payload: PlanPayload } | null>;
  payload: PlanPayload | null;
  onPlan: (payload: PlanPayload | null) => void;
  onSelectFinding?: (finding: Finding) => void;
  joints?: RobotJoint[];
  selected?: number | null;
  onSelect?: (index: number | null) => void;
}

const baseName = (path: string) => path.split(/[\\/]/).pop() || path;

/** One right-hand Panel: pick a plan, see the verdict and its findings. The producer owns every
 * displayed result; the router keeps it across tab changes. */
export function PlanInput({ onChoosePlan, payload, onPlan, onSelectFinding, joints, selected, onSelect }: PlanInputProps) {
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

  return (
    <Panel title="Plan" actions={payload && <span className="verdict-word" role="status">{verdictPill(payload.verdict)}</span>}>
      <div className="plan-input">
        <FilePick label="Choose plan" path={plan ? baseName(plan) : undefined} onPick={choosePlan} onRemove={() => { setPlan(null); onSelect?.(null); onPlan(null); }} />
      </div>
      {!plan && !payload && !error && <EmptyState title="No plan chosen" />}
      {error && <Notice kind="error">Plan failed: {error}</Notice>}
      {payload && <PlanFace payload={payload} joints={joints} selected={selected} onSelect={onSelect} onSelectFinding={onSelectFinding} />}
    </Panel>
  );
}
