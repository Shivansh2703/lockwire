import { useEffect, useRef, useState } from "react";

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
  /** Lets the slider write back into the form's value field (never to disk). Without it there is no slider. */
  onValue?: (value: string) => void;
}

/** The check takes seconds on a real robot (measured 1.7 s on servo-bench, over the 300 ms line), so the
 * preview runs when typing or dragging pauses, never per keystroke. */
export const PREVIEW_PAUSE_MS = 600;

/** The number in "1.5 kg" or "1.5"; null for vectors and text. */
const scalar = (v: MeasureCompareObservation["value"]): number | null => {
  const m = /^\s*(-?\d+(?:\.\d+)?)(?:\s*[A-Za-z*/^%]+)?\s*$/.exec(valueText(v));
  return m ? Number(m[1]) : null;
};

function valueText(value: string | number | readonly number[]): string {
  return Array.isArray(value) ? value.join(",") : String(value);
}

const pill = (verdict: string) => {
  const [status, label] = VERDICT[verdict] ?? ["muted", verdict];
  return <StatusPill status={status}>{label}</StatusPill>;
};

export function MeasureCompareDeclare({ observation: selected, contractPath, expectedRevision, onValue }: MeasureCompareDeclareProps) {
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

  const seq = useRef(0);
  /** `preview`: no source or witness typed yet, so the route gets a stand-in; nothing is saved either way. */
  async function compare(preview = false) {
    const source = selected?.source?.trim() || (preview ? "preview" : "");
    const witness = selected?.witness?.trim() || (preview ? "preview" : "");
    if (!selected || !source || !witness || !contractPath || !expectedRevision) return;
    const mine = ++seq.current;
    setCompareStatus("comparing");
    setCompareRefusal(null);
    try {
      const result = await window.lockwire.compareContractWhatIf({
        contract: contractPath,
        expected_revision: expectedRevision,
        change: { path: selected.path, value: valueText(selected.value), source, witness },
      });
      if (mine !== seq.current) return; // a newer pause already asked
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
      if (mine !== seq.current) return;
      setComparison(null);
      setCompareStatus("refused");
      setCompareRefusal(error instanceof Error ? error.message : String(error));
    }
  }

  // Live preview: the typed value, after a pause. The first render is the saved value, so it asks nothing.
  const typed = selected ? valueText(selected.value) : "";
  const first = useRef(typed);
  useEffect(() => {
    seq.current++; // typing again retires any answer for the old value
    setCompareStatus("idle");
    if (typed === first.current || typed.trim() === "" || !contractPath || !expectedRevision) return;
    const timer = setTimeout(() => void compare(true), PREVIEW_PAUSE_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [typed, selected?.source, selected?.witness, contractPath, expectedRevision]);

  const num = selected ? scalar(selected.value) : null;
  const top = useRef(0);
  if (num !== null && num * 2 > top.current) top.current = num * 2;
  const reach = top.current || 1;

  return (
    <>
      {onValue && num !== null && (
        <label className="measure-slider-label">Slide to see where the verdict flips
          <input type="range" className="measure-slider" aria-label="Slide the value to see where the verdict flips" min={0} max={reach} step={reach / 100} value={num} onChange={(e) => onValue(String(Number(e.target.value)))} />
        </label>
      )}
      <Button onClick={() => compare()} disabled={!canCompare || compareStatus === "comparing"} title={canCompare ? undefined : "needs a source and a Checked by name"}>
        Compare
      </Button>
      {compareStatus === "comparing" && <div className="declare-compare" role="status">Checking your change...</div>}
      {compareStatus === "ready" && comparison && (
        <div className="declare-compare measure-compare-result">
          <div className="declare-compare-verdicts"><strong className="measure-preview-note">Preview, not saved</strong> {pill(comparison.current)} <span aria-hidden="true">{"→"}</span> {pill(comparison.proposed)}</div>
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
  const kgPoint = kg.trim().replace(/^(\d+),(\d+)$/, "$1.$2"); // one decimal comma reads as a point
  async function compare() {
    setState("busy");
    const refused = await onCompare(kgPoint, source.trim()).catch((error: unknown) => (error instanceof Error ? error.message : String(error)));
    setRefusal(refused);
    setState(refused ? "idle" : "done");
  }
  return (
    <Panel title="Compare with datasheet">
      <div className="datasheet-compare">
        <p className="datasheet-compare-sub">Type what the manufacturer says. {PRODUCT_NAME} checks it against your model.</p>
        <Field label="Datasheet weight (kg)" aria-label="datasheet weight" inputMode="decimal" value={kg} onChange={(e: { target: { value: string } }) => setKg(e.target.value)} />
        <Field label="Source" placeholder="URL or text" aria-label="datasheet source" value={source} onChange={(e: { target: { value: string } }) => setSource(e.target.value)} />
        <Button variant="primary" onClick={compare} disabled={!(Number(kgPoint) > 0) || !source.trim() || state === "busy"}>Compare</Button>
        {state === "done" && <div className="datasheet-compare-result" role="status">{result ?? "Model matches the datasheet within \u00b15 %"}</div>}
        {refusal && <Notice kind="error">{refusal}</Notice>}
      </div>
    </Panel>
  );
}
