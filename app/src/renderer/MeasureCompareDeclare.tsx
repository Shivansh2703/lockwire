import { useState } from "react";

import { PRODUCT_NAME } from "../shared/branding";

import type { Card } from "./checkFacePayload";
import { Button, Disclosure, Field, Notice, Panel, StatusPill } from "./kit";
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

/** The robot's datasheet mass, as `lockwire check --mass` writes it (start_operation.add_mass_check). */
export const MASS_NOMINAL = "budgets/bud.mass/nominal";
const short = (n: number) => Number(n.toPrecision(3));

/** "Model 1.5 kg vs datasheet 1.63 kg (−8 %)" from the mass budget's own figures; null without them. */
export function massLine(card: Card): string | null {
  const { observed: o, expected: e } = card;
  if (!o || !e || !e.value) return null;
  const pct = Math.round(((o.value - e.value) / e.value) * 100);
  return `Model ${short(o.value)} ${o.unit} vs datasheet ${short(e.value)} ${e.unit} (${pct < 0 ? "\u2212" : "+"}${Math.abs(pct)} %)`;
}

/** Screen 3B: one typed weight, one tap. `onCompare` writes the budget and re-checks; it returns a refusal or null. */
export function CompareWithDatasheet({ result, onCompare }: { result: string | null; onCompare: (kg: string, source: string) => Promise<string | null> }) {
  const [kg, setKg] = useState("");
  const [source, setSource] = useState("datasheet");
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [refusal, setRefusal] = useState<string | null>(null);
  async function compare() {
    setState("busy");
    const refused = await onCompare(kg.trim(), source.trim()).catch((error: unknown) => (error instanceof Error ? error.message : String(error)));
    setRefusal(refused);
    setState(refused ? "idle" : "done");
  }
  return (
    <Panel title="Compare with datasheet">
      <div className="datasheet-compare">
        <p className="datasheet-compare-sub">Type what the manufacturer says. {PRODUCT_NAME} checks it against your model.</p>
        <Field label="Datasheet weight (kg)" aria-label="datasheet weight" inputMode="decimal" value={kg} onChange={(e: { target: { value: string } }) => setKg(e.target.value)} />
        <Field label="Source" placeholder="URL or text" aria-label="datasheet source" value={source} onChange={(e: { target: { value: string } }) => setSource(e.target.value)} />
        <Button variant="primary" onClick={compare} disabled={!(Number(kg) > 0) || !source.trim() || state === "busy"}>Compare</Button>
        {state === "done" && <div className="datasheet-compare-result" role="status">{result ?? "Model matches the datasheet within \u00b15 %"}</div>}
        {refusal && <Notice kind="error">{refusal}</Notice>}
      </div>
    </Panel>
  );
}
