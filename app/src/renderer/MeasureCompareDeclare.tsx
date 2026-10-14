import { useState } from "react";

import { Button, Disclosure, Notice, StatusPill } from "./kit";
import { VERDICT } from "./FindingList";

/** The proposal under the Declare form: the value, source and witness about to be declared.
 * Compare sends it to the named read-only what-if route; it never measures, infers or writes. */
export interface MeasureCompareObservation {
  path: string;
  value: string | number | readonly number[];
  source: string | null;
  witness: string | null;
}

export interface MeasureCompareDeclareProps {
  observation: MeasureCompareObservation | null;
  /** The app-owned document identity. Without both fields comparison stays disabled. */
  contractPath?: string;
  expectedRevision?: string;
}

function valueText(value: string | number | readonly number[]): string {
  return Array.isArray(value) ? value.join(",") : String(value);
}

const pill = (verdict: string) => {
  const [status, label] = VERDICT[verdict] ?? ["muted", verdict];
  return <StatusPill status={status}>{label}</StatusPill>;
};

export function MeasureCompareDeclare({ observation: selected, contractPath, expectedRevision }: MeasureCompareDeclareProps) {
  const [compareStatus, setCompareStatus] = useState<"idle" | "comparing" | "ready" | "refused">("idle");
  const [comparison, setComparison] = useState<{
    current: string;
    proposed: string;
    currentScored: number | null;
    proposedScored: number | null;
    currentFindings: number;
    proposedFindings: number;
    diff: string;
  } | null>(null);
  const [compareRefusal, setCompareRefusal] = useState<string | null>(null);
  const canCompare = Boolean(selected?.source?.trim() && selected.witness?.trim() && contractPath && expectedRevision);

  async function compare() {
    if (!selected || !canCompare || !selected.witness || !selected.source || !contractPath || !expectedRevision) return;
    setCompareStatus("comparing");
    setCompareRefusal(null);
    try {
      const result = await window.lockwire.compareContractWhatIf({
        contract: contractPath,
        expected_revision: expectedRevision,
        change: {
          path: selected.path,
          value: valueText(selected.value),
          source: selected.source,
          witness: selected.witness,
        },
      });
      setComparison({
        current: result.current.verdict,
        proposed: result.proposed.verdict,
        currentScored: result.current.scored,
        proposedScored: result.proposed.scored,
        currentFindings: result.current.findings.length,
        proposedFindings: result.proposed.findings.length,
        diff: result.diff,
      });
      setCompareStatus("ready");
    } catch (error) {
      setComparison(null);
      setCompareStatus("refused");
      setCompareRefusal(error instanceof Error ? error.message : String(error));
    }
  }

  return (
    <>
      <Button onClick={compare} disabled={!canCompare || compareStatus === "comparing"} title={canCompare ? undefined : "needs a source and a witness"}>
        Compare
      </Button>
      {compareStatus === "ready" && comparison && (
        <div className="declare-compare measure-compare-result">
          <div className="declare-compare-verdicts">{pill(comparison.current)} <span aria-hidden="true">{"→"}</span> {pill(comparison.proposed)}</div>
          <Disclosure summary="Diff">
            <p>
              scored {comparison.currentScored ?? "unscored"} {"→"} {comparison.proposedScored ?? "unscored"}; findings {comparison.currentFindings} {"→"} {comparison.proposedFindings}
            </p>
            <pre className="finding-raw">{comparison.diff}</pre>
          </Disclosure>
        </div>
      )}
      {compareStatus === "refused" && <div className="declare-compare"><Notice kind="error">{compareRefusal}</Notice></div>}
    </>
  );
}
