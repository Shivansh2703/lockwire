import { useState } from "react";

import type { AddPartPayload } from "../shared/api.gen";
import type { DeclareSubmitResult } from "./DeclareFace";
import { Button, Disclosure, Field, Notice } from "./kit";

// Add one powered part (the served `add device`): a name, its draw in watts, the rail it sits on,
// and who checked it. Source defaults to "datasheet". The family and model are made from the name
// by the backend in the same write, so the form never asks for them.

export interface AddPartSubmission {
  id: string;
  draw: string;
  rail: string;
  source: string;
  witness: string;
}

const EMPTY: AddPartPayload = { rails: [], parts: [] };
const NUMBER_NAMES: Record<string, string> = { draw_max: "Power draw", input_voltage_min: "Lowest voltage", input_voltage_max: "Highest voltage" };
const numberName = (key: string) => NUMBER_NAMES[key] ?? (key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, " "));
/** "NVIDIA Jetson Orin Nano module datasheet + ..." -> "NVIDIA datasheet": the maker and what kind of source; the full note lives under Sources. */
const sourceName = (source: string) => `${source.split(/\s/)[0]} ${/datasheet|data sheet/i.test(source) ? "datasheet" : "source"}`;

export function AddPartForm({ witness: prefill, catalog = EMPTY, onAdd }: { witness: string; catalog?: AddPartPayload; onAdd: (part: AddPartSubmission) => Promise<DeclareSubmitResult> }) {
  const [pick, setPick] = useState("");
  const part = catalog.parts.find((p) => p.id === pick);
  const [id, setId] = useState("");
  const [draw, setDraw] = useState("");
  const [rail, setRail] = useState("");
  const [source, setSource] = useState("datasheet");
  const [witness, setWitness] = useState(prefill);
  const [result, setResult] = useState<DeclareSubmitResult | "busy" | null>(null);
  const ready = [id, draw, rail, source, witness].every((v) => v.trim() !== "");
  // Picking a part fills the form from its sourced numbers; the rail is preselected only when exactly
  // one rail's voltage fits the part, never guessed.
  function choose(value: string) {
    setPick(value);
    const next = catalog.parts.find((p) => p.id === value);
    const drawNumber = next?.numbers.find((n) => n.key === "draw_max");
    setId(next ? `dev.${next.id.split(".").pop()}` : "");
    setDraw(drawNumber ? String(drawNumber.value) : "");
    setRail(next?.fits.length === 1 ? next.fits[0]! : "");
    setSource(drawNumber ? drawNumber.source : "datasheet");
  }
  const text = (set: (v: string) => void) => (event: { target: { value: string } }) => set(event.target.value);

  async function submit() {
    setResult("busy");
    try {
      const done = await onAdd({ id: id.trim(), draw: draw.trim(), rail: rail.trim(), source: source.trim(), witness: witness.trim() });
      setResult(done);
      if (done.written) { setId(""); setDraw(""); }
    } catch (error) {
      setResult({ written: false, refusal: error instanceof Error ? error.message : String(error) });
    }
  }

  return (
    <div className="declare-review add-part" aria-label="Add part">
      {catalog.parts.length > 0 && (
        <Field label="Part" control="select" aria-label="catalog part" value={pick} onChange={(e: { target: { value: string } }) => choose(e.target.value)}>
          <option value="">Custom part</option>
          {catalog.parts.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </Field>
      )}
      {part && (
        <ul className="add-part-numbers" aria-label="catalog numbers">
          {part.numbers.map((n) => (
            <li key={n.key}>{numberName(n.key)} {n.value} {n.unit}{n.kind === "assumed" ? " (assumed)" : ""} · {sourceName(n.source)}</li>
          ))}
        </ul>
      )}
      {catalog.rails.length > 0
        ? (
          <Field label="Rail" control="select" aria-label="rail" value={rail} onChange={text(setRail)}>
            <option value="">Pick a rail</option>
            {catalog.rails.map((r) => <option key={r} value={r}>{r}</option>)}
          </Field>
        )
        : <Field label="Rail" placeholder="e.g. rail.servo-5v" aria-label="rail" value={rail} onChange={text(setRail)} />}
      {part && !rail && <small className="add-part-help">{part.fits.length > 1 ? "Several rails match this part's voltage" : "No rail matches this part's voltage"} — pick one</small>}
      <Button variant="primary" onClick={submit} disabled={!ready || result === "busy"}>Add part</Button>
      {result && result !== "busy" && (result.written
        ? <Notice kind="info">Part added</Notice>
        : <Notice kind="error">{result.refusal ?? "Refused"}</Notice>)}
      <Disclosure summary="Details" open={!part || undefined}>
        <div className="declare-review">
          <Field label="Part name" placeholder="e.g. dev.servo5" aria-label="part name" value={id} onChange={text(setId)} />
          <Field label="Power draw (W)" aria-label="power draw" value={draw} onChange={text(setDraw)} />
          <Field label="Source" aria-label="part source" value={source} onChange={text(setSource)} />
          <Field label="Witness" hint="Who checked it" aria-label="part witness" value={witness} onChange={text(setWitness)} />
        </div>
      </Disclosure>
      {part && (
        <Disclosure summary="Sources">
          <ul className="add-part-sources">
            {part.numbers.map((n) => <li key={n.key}><b>{n.key}</b> {n.value} {n.unit}: {n.source}</li>)}
          </ul>
        </Disclosure>
      )}
    </div>
  );
}
