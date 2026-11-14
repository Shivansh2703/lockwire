import { useState } from "react";

import type { RobotParam } from "../shared/api.gen";
import type { DeclareSubmitResult } from "./DeclareFace";
import { Button, Field, Notice } from "./kit";
import { lenToMm, lenUnit, shown, useUnits } from "./units";

// Add one shape (a box, plate, cylinder, tube or sphere) as a new link: a name and its sizes. A size
// is a number in mm or the name of a named size the robot already has. The model has box, cylinder
// and sphere only, so a plate is a thin box and a tube is drawn as a solid cylinder.

export type ShapeKind = "box" | "plate" | "cylinder" | "tube" | "sphere";
/** `newSizes` are named sizes typed as `name=220` (mm): the app adds each before the shape that uses it. */
export interface ShapeSubmission { name: string; geometry: Record<string, unknown>[]; attachTo: string; witness: string; newSizes: { name: string; mm: number }[] }

const DIMS: Record<ShapeKind, string[]> = {
  box: ["Length", "Width", "Height"], plate: ["Length", "Width", "Thickness"], cylinder: ["Radius", "Length"], tube: ["Radius", "Length"], sphere: ["Radius"],
};
const KINDS: [ShapeKind, string][] = [["box", "Box"], ["plate", "Plate"], ["cylinder", "Cylinder"], ["tube", "Tube"], ["sphere", "Sphere"]];

/** One typed size: a plain number is in the shown length unit (sent as metres), a declared name stays a name. Null when it is neither. */
export function sizeOf(text: string, names: string[]): number | string | null {
  const t = text.trim();
  if (names.includes(t)) return t;
  return /^\d+(\.\d+)?$/.test(t) && Number(t) > 0 ? lenToMm(Number(t)) / 1000 : null;
}

const NEW_SIZE = /^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\d+(\.\d+)?)$/;
/** `arm_length=220` makes a new named size of 220 mm; null for anything else. */
export function newSizeOf(text: string): { name: string; mm: number } | null {
  const m = NEW_SIZE.exec(text.trim());
  return m && Number(m[2]) > 0 ? { name: m[1]!, mm: lenToMm(Number(m[2])) } : null;
}

/** The link's `geometry` for a kind and its typed sizes, in the order DIMS lists them. */
export function geometryOf(kind: ShapeKind, sizes: (number | string)[]): Record<string, unknown>[] {
  if (kind === "box" || kind === "plate") return [{ kind: "box", size: sizes }];
  if (kind === "sphere") return [{ kind: "sphere", radius: sizes[0] }];
  return [{ kind: "cylinder", radius: sizes[0], length: sizes[1] }];
}

export function AddShapeForm({ witness: prefill, links, params, onAdd }: { witness: string; links: string[]; params: RobotParam[]; onAdd: (shape: ShapeSubmission) => Promise<DeclareSubmitResult>}) {
  useUnits();
  const [kind, setKind] = useState<ShapeKind>("box");
  const [name, setName] = useState("");
  const [typed, setTyped] = useState<string[]>([]);
  const [attachTo, setAttachTo] = useState("");
  const [result, setResult] = useState<DeclareSubmitResult | "busy" | null>(null);
  const [tried, setTried] = useState(false);
  const names = params.map((p) => p.name);
  const fresh = DIMS[kind].map((_, i) => newSizeOf(typed[i] ?? ""));
  const newSizes = fresh.filter((n): n is { name: string; mm: number } => n !== null).filter((n, i, all) => all.findIndex((o) => o.name === n.name) === i);
  const clash = newSizes.find((n) => names.includes(n.name));
  const sizes = DIMS[kind].map((_, i) => fresh[i]?.name ?? sizeOf(typed[i] ?? "", names));
  const taken = links.includes(name.trim());
  const problem = !name.trim() ? "Give the shape a name." : taken ? `A link called ${name.trim()} already exists.` : clash ? `${clash.name} is already a named size. Change it under Named sizes.` : null;

  async function submit() {
    setTried(true);
    if (problem || sizes.some((s) => s === null)) return;
    setResult("busy");
    try {
      const done = await onAdd({ name: name.trim(), geometry: geometryOf(kind, sizes as (number | string)[]), attachTo, witness: prefill, newSizes });
      setResult(done);
      if (done.written) { setName(""); setTyped([]); setTried(false); }
    } catch (error) {
      setResult({ written: false, refusal: error instanceof Error ? error.message : String(error) });
    }
  }
  return (
    <div className="declare-review add-part" aria-label="Add shape">
      <Field label="Shape" control="select" aria-label="shape type" value={kind} onChange={(e: { target: { value: string } }) => { setKind(e.target.value as ShapeKind); setTyped([]); }}>
        {KINDS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
      </Field>
      <Field label="Name" placeholder="e.g. upper_arm" aria-label="shape name" value={name} onChange={(e: { target: { value: string } }) => setName(e.target.value)} error={tried ? problem ?? undefined : undefined} />
      {DIMS[kind].map((label, i) => (
        <Field key={`${kind}-${label}`} label={`${label} (${lenUnit()})`} aria-label={label.toLowerCase()} placeholder={names.length ? `${lenUnit()} or ${names[0]}` : `${lenUnit()}, or name=${lenUnit()}`} value={typed[i] ?? ""}
          onChange={(e: { target: { value: string } }) => setTyped((t) => { const next = [...t]; next[i] = e.target.value; return next; })}
          error={tried && sizes[i] === null ? (names.length ? `Type a size in ${lenUnit()} or a named size.` : `Type a size in ${lenUnit()}, or name=${lenUnit()} for a new named size.`) : undefined} />
      ))}
      {links.length > 0 && (
        <Field label="Attach to" control="select" aria-label="attach to" value={attachTo} onChange={(e: { target: { value: string } }) => setAttachTo(e.target.value)}>
          <option value="">Nothing yet</option>
          {links.map((l) => <option key={l} value={l}>{l}</option>)}
        </Field>
      )}
      {names.length > 0 && <small className="add-part-help">Named sizes: {params.map((p) => `${p.name} ${shown(p.value, p.unit).value} ${shown(p.value, p.unit).unit}`).join(", ")}</small>}
      <Button variant="primary" onClick={() => void submit()} disabled={result === "busy"}>Add shape</Button>
      {result && result !== "busy" && (result.written ? <Notice kind="info">Shape added</Notice> : <Notice kind="error">{result.refusal ?? "Refused"}</Notice>)}
    </div>
  );
}
