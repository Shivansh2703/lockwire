import { useRef, useState } from "react";

import type { AddPartPayload, KitOption, StockOption } from "../shared/api.gen";
import type { DeclareSubmitResult } from "./DeclareFace";
import { Button, Disclosure, Field, Notice } from "./kit";
import { lenToMm, lenUnit, useUnits } from "./units";

// Add one powered part (the served `add device`): a name, its draw in watts, the rail it sits on,
// and who checked it. Source defaults to "datasheet". The family and model are made from the name
// by the backend in the same write, so the form never asks for them.

export interface AddPartSubmission {
  id: string;
  draw: string;
  rail: string;
  source: string;
  witness: string;
  /** No rail fits: make this one first (typed by the user, never defaulted). */
  newRail?: { id: string; voltage: string; capacity: string };
  /** CAD-05: also hang the part on a link; the device add stays, so its rail and draw are still checked. */
  attach?: StructureSubmission;
}

/** LIB-4: a stock shape or kit part for the Structure tab. Lengths are metres (typed in mm); the position is metres from the link it hangs on. */
export interface StructureSubmission {
  kind: "stock" | "attach";
  /** the catalog id (stock.plate, gobilda.1501-...) */
  part: string;
  name: string;
  dimensions: Record<string, number>;
  material: string;
  parent: string;
  xyz: [number, number, number];
  witness: string;
  /** CAD-05: the part's own mounting frame (the engine applies its position and orientation); only sent when the part has one. */
  frame?: string;
}
export interface MaterialChoice { name: string; density_kg_m3: number }

const EMPTY: AddPartPayload = { rails: [], parts: [] };
const NUMBER_NAMES: Record<string, string> = { draw_max: "Power draw", input_voltage_min: "Lowest voltage", input_voltage_max: "Highest voltage" };
const numberName = (key: string) => NUMBER_NAMES[key] ?? (key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, " "));
/** "NVIDIA Jetson Orin Nano module datasheet + ..." -> "NVIDIA datasheet": the maker and what kind of source; the full note lives under Sources. */
const sourceName = (source: string) => `${source.split(/\s/)[0]} ${/datasheet|data sheet/i.test(source) ? "datasheet" : "source"}`;

const words = (key: string) => key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, " ");
const hit = (q: string, name: string) => name.toLowerCase().includes(q.trim().toLowerCase());
const signedNumber = (v: string) => /^-?\d+(\.\d+)?$/.test(v.trim());

/** The engine's `add` fields for a Structure submission; a part's frame goes as its id, so the engine applies the frame's position and orientation. */
export const structureFields = (s: StructureSubmission) => ({ name: s.name, parent: s.parent, frame: { xyz: s.xyz }, ...(s.frame && { frame_id: s.frame }), joint: { type: "fixed" }, ...(s.kind === "stock" ? { stock: s.part, dimensions: s.dimensions, ...(s.material && { material: s.material }) } : { part: s.part }) });

/** CAD-05: the attach that follows a device add. Refused: undo the writes this add made (the device, and the rail when it made one) and say so. Returns the refusal text, or null when attached. */
export async function attachAfterDevice(a: { attach: StructureSubmission; revision: string; railMade: boolean; write: (revision: string, kind: string, id: string, fields: Record<string, unknown>, source: string) => Promise<{ written: boolean; refusal: string | null }>; undo: () => Promise<boolean> }): Promise<string | null> {
  const att = await a.write(a.revision, "attach", a.attach.name, structureFields(a.attach), "picked in the app");
  if (att.written) return null;
  let undone = true;
  for (let i = 0; i < (a.railMade ? 2 : 1); i++) undone = (await a.undo()) && undone;
  return `${att.refusal ?? "Not attached."} ${undone ? "Nothing was added." : "The part was added but could not be taken back; use Undo."}`;
}

const uniqueName = (pick: string, links: string[]) => { const base = pick.split(".").pop()!.replace(/[^a-z0-9]+/gi, "_").replace(/^_|_$/g, ""); let n = base, i = 2; while (links.includes(n)) n = `${base}_${i++}`; return n; };

/** Where a part hangs: the link and a typed X/Y/Z in mm. `frames` (catalog components only): offer the part's mounting point, or say it has none. */
function PlaceFields({ links, parent, setParent, xyz, setXyz, none, frames, snap, setSnap }: { links: string[]; parent: string; setParent: (v: string) => void; xyz: string[]; setXyz: (f: (v: string[]) => string[]) => void; none?: boolean; frames?: string[]; snap?: boolean; setSnap?: (v: boolean) => void }) {
  useUnits(); // the one Units setting: typed numbers are in it, requests go out in metres
  return (
    <>
      <Field label="Attach to" control="select" aria-label="structure attach to" value={parent} onChange={(e: { target: { value: string } }) => setParent(e.target.value)}>
        {none && <option value="">Not attached</option>}
        {links.map((l) => <option key={l} value={l}>{l}</option>)}
      </Field>
      {(!none || parent) && (
        <Disclosure summary="Position" open={none || undefined}>
          <div className="declare-review">
            {(["X", "Y", "Z"] as const).map((axis, i) => <Field key={axis} label={`${axis} (${lenUnit()})`} aria-label={`position ${axis.toLowerCase()}`} value={xyz[i]!} onChange={(e: { target: { value: string } }) => setXyz((v) => v.map((x, j) => (j === i ? e.target.value : x)))} />)}
          </div>
        </Disclosure>
      )}
      {frames && parent && (frames.length > 0
        ? <label><input type="checkbox" aria-label="snap to mounting point" checked={!!snap} onChange={(e) => setSnap?.(e.target.checked)} /> Snap to its mounting point</label>
        : <small className="add-part-help" aria-label="no mounting frame">No mounting frame on record for this part.</small>)}
    </>
  );
}

/** The Structure tab: stock shapes (sized by typed dimensions, in mm) and kit parts, added through the same write as the other adds. */
function StructureTab({ stock, kit, links, materials, witness, onAdd, query }: { stock: StockOption[]; kit: KitOption[]; links: string[]; materials: MaterialChoice[]; witness: string; onAdd: (s: StructureSubmission) => Promise<DeclareSubmitResult>; query: string }) {
  const [pick, setPick] = useState("");
  const [dims, setDims] = useState<Record<string, string>>({});
  const [material, setMaterial] = useState("");
  const [parent, setParent] = useState(links[0] ?? "");
  const [xyz, setXyz] = useState(["0", "0", "0"]);
  const [shown, setShown] = useState(false);
  const [result, setResult] = useState<DeclareSubmitResult | "busy" | null>(null);
  const shape = stock.find((s) => s.id === pick);
  const kitPart = kit.find((k) => k.id === pick);
  const outline = (shape ?? kitPart)?.outline_only;
  const needsMaterial = shape?.mass_from === "material";
  const bad = (shape?.dimensions ?? []).some((d) => !/^\d+(\.\d+)?$/.test((dims[d] ?? "").trim()) || Number(dims[d]) <= 0);
  const problem = !pick ? "Pick a shape or part." : bad ? "Type every size, as a number above 0." : needsMaterial && !material ? "Pick a material." : !parent ? "Pick what it attaches to." : !xyz.every(signedNumber) ? "Type the position as numbers." : null;
  const name = uniqueName(pick, links);
  async function submit() {
    setShown(true);
    if (problem) return;
    setResult("busy");
    try {
      const done = await onAdd({
        kind: shape ? "stock" : "attach", part: pick, name,
        dimensions: Object.fromEntries((shape?.dimensions ?? []).map((d) => [d, Number(dims[d]) / 1000])),
        material: needsMaterial ? material : "", parent, xyz: xyz.map((v) => lenToMm(Number(v)) / 1000) as [number, number, number], witness,
      });
      setResult(done);
      if (done.written) { setPick(""); setDims({}); setShown(false); }
    } catch (error) {
      setResult({ written: false, refusal: error instanceof Error ? error.message : String(error) });
    }
  }
  const stockHits = stock.filter((s) => hit(query, s.name)), kitHits = kit.filter((k) => hit(query, k.name));
  return (
    <div className="declare-review" aria-label="Structure">
      <Field label="Shape or part" control="select" aria-label="structure part" value={pick} onChange={(e: { target: { value: string } }) => { setPick(e.target.value); setDims({}); setResult(null); }}>
        <option value="">Pick one</option>
        {stockHits.length > 0 && <optgroup label="Stock shapes">{stockHits.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</optgroup>}
        {kitHits.length > 0 && <optgroup label="Kit parts">{kitHits.map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}</optgroup>}
      </Field>
      {outline && <small className="add-part-help" aria-label="outline only">Outline only: drawn as its outer box</small>}
      {shape && <small className="add-part-help" aria-label="mass source">{needsMaterial ? "Mass is derived from the size and the material" : "Mass is the maker's figure per metre, times the length"}</small>}
      {shape?.dimensions.map((d) => <Field key={d} label={`${words(d)} (mm)`} aria-label={`size ${d}`} value={dims[d] ?? ""} onChange={(e: { target: { value: string } }) => setDims((m) => ({ ...m, [d]: e.target.value }))} />)}
      {needsMaterial && (
        <Field label="Material" control="select" aria-label="structure material" value={material} onChange={(e: { target: { value: string } }) => setMaterial(e.target.value)}>
          <option value="">Pick a material</option>
          {materials.map((m) => <option key={m.name} value={m.name}>{`${m.name} (${m.density_kg_m3} kg/m³)`}</option>)}
        </Field>
      )}
      <PlaceFields links={links} parent={parent} setParent={setParent} xyz={xyz} setXyz={setXyz} />
      {shown && problem && <Notice kind="error">{problem}</Notice>}
      <Button variant="primary" onClick={() => void submit()} disabled={result === "busy"}>Add part</Button>
      {result && result !== "busy" && (result.written ? <Notice kind="info">Part added</Notice> : <Notice kind="error">{result.refusal ?? "Refused"}</Notice>)}
    </div>
  );
}

export function AddPartForm({ witness: prefill, catalog = EMPTY, onAdd, askWitness = true, onAddStructure, links = [], materials = [] }: { witness: string; catalog?: AddPartPayload; onAdd: (part: AddPartSubmission) => Promise<DeclareSubmitResult>; askWitness?: boolean; onAddStructure?: (s: StructureSubmission) => Promise<DeclareSubmitResult>; links?: string[]; materials?: MaterialChoice[] }) {
  const [tab, setTab] = useState<"Parts" | "Structure">("Parts");
  const [query, setQuery] = useState("");
  const stock = catalog.stock ?? [], kit = catalog.kit ?? [];
  const tabbed = !!onAddStructure && stock.length + kit.length > 0;
  const shownParts = tabbed ? catalog.parts.filter((p) => hit(query, p.name)) : catalog.parts;
  const structureCount = stock.filter((s) => hit(query, s.name)).length + kit.filter((k) => hit(query, k.name)).length;
  const [pick, setPick] = useState("");
  const part = catalog.parts.find((p) => p.id === pick);
  const [id, setId] = useState("");
  const [draw, setDraw] = useState("");
  const [rail, setRail] = useState("");
  const [source, setSource] = useState("datasheet");
  const [witness, setWitness] = useState(prefill);
  const [result, setResult] = useState<DeclareSubmitResult | "busy" | null>(null);
  const [railName, setRailName] = useState("main");
  const [volt, setVolt] = useState("");
  const [cap, setCap] = useState("");
  const [attachTo, setAttachTo] = useState(""); // CAD-05: "" = just add the part; a link = attach it there instead
  const [attXyz, setAttXyz] = useState(["0", "0", "0"]);
  const [snap, setSnap] = useState(false);
  const [attachError, setAttachError] = useState(false);
  const canAttach = !!part && links.length > 0;
  const noFit = catalog.rails.length === 0 || (!!part && part.fits.length === 0);
  const makeRail = noFit && !rail;
  const newId = `rail.${railName.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "")}`;
  const taken = catalog.rails.includes(newId);
  const plainNumber = (v: string) => /^\d+(\.\d+)?$/.test(v.trim()) && Number(v) > 0; // "1e3", "0x10", "Infinity" are not numbers here
  const err: Record<string, string | null> = {
    rail: !makeRail && !rail.trim() ? "Pick a rail." : null,
    rname: makeRail ? (newId === "rail." ? "Give the rail a name." : taken ? `A rail called ${newId} already exists.` : null) : null,
    volt: makeRail && !plainNumber(volt) ? "Type the voltage." : null,
    cap: makeRail && !plainNumber(cap) ? "Type the most it can deliver." : null,
    id: id.trim() ? null : "Give the part a name.",
    draw: plainNumber(draw) ? null : "Type how much power it draws.",
    source: source.trim() ? null : "Say where the numbers came from.",
    witness: askWitness && !witness.trim() ? "Type who checked it." : null,
  };
  // Keys in visual order, with the aria-label of their field.
  const ORDER = [["rail", "rail"], ["rname", "rail name"], ["volt", "rail voltage"], ["cap", "rail capacity"], ["id", "part name"], ["draw", "power draw"], ["source", "part source"], ["witness", "part checked by"]] as const;
  const [seen, setSeen] = useState<string[]>([]); // a line shows once its field was left, or Add was pressed
  const root = useRef<HTMLDivElement>(null);
  // The kit Field renders the line as role="alert" with aria-invalid/aria-describedby wired.
  const f = (k: string) => ({ error: seen.includes(k) ? (err[k] ?? undefined) : undefined, onBlur: () => setSeen((s) => (s.includes(k) ? s : [...s, k])) });
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
    if (canAttach && attachTo && !attXyz.every(signedNumber)) { setAttachError(true); return; }
    setAttachError(false);
    const first = ORDER.find(([k]) => err[k]);
    if (first) {
      setSeen(ORDER.map(([k]) => k));
      const el = root.current?.querySelector<HTMLElement>(`[aria-label="${first[1]}"]`);
      el?.closest("details")?.setAttribute("open", ""); // a collapsed Details cannot take focus
      el?.focus();
      return;
    }
    setResult("busy");
    try {
      const done = await onAdd({ id: id.trim(), draw: draw.trim(), rail: makeRail ? newId : rail.trim(), source: source.trim(), witness: witness.trim(), ...(canAttach && attachTo && { attach: { kind: "attach" as const, part: pick, name: uniqueName(pick, links), dimensions: {}, material: "", parent: attachTo, xyz: attXyz.map((v) => lenToMm(Number(v)) / 1000) as [number, number, number], witness: witness.trim(), ...(snap && (part!.frames ?? []).length > 0 && { frame: part!.frames![0] }) } }), ...(makeRail && { newRail: { id: newId, voltage: `${volt.trim()} V`, capacity: `${cap.trim()} W` } }) });
      setResult(done);
      if (!done.written && done.railKept) setRail(newId); // the rail stays: offer it in the select, never ask to make it again
      if (done.written) { setId(""); setDraw(""); setSeen([]); }
    } catch (error) {
      setResult({ written: false, refusal: error instanceof Error ? error.message : String(error) });
    }
  }

  const tabs = tabbed && (
    <>
      <input type="search" aria-label="search parts" placeholder="Search parts" value={query} onChange={(e) => setQuery(e.target.value)} />
      <div role="tablist" aria-label="Part kind" className="add-part-tabs">
        {(["Parts", "Structure"] as const).map((t) => {
          const n = t === "Parts" ? shownParts.length : structureCount;
          return <Button key={t} variant={tab === t ? "primary" : "quiet"} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>{query.trim() ? `${t} (${n})` : t}</Button>;
        })}
      </div>
    </>
  );
  if (tabbed && tab === "Structure") {
    return <div className="declare-review add-part" aria-label="Add part">{tabs}<StructureTab stock={stock} kit={kit} links={links} materials={materials} witness={witness} onAdd={onAddStructure!} query={query} /></div>;
  }
  return (
    <div className="declare-review add-part" aria-label="Add part" ref={root}>
      {tabs}
      {catalog.parts.length > 0 && (
        <Field label="Part" control="select" aria-label="catalog part" value={pick} onChange={(e: { target: { value: string } }) => choose(e.target.value)}>
          <option value="">Custom part</option>
          {shownParts.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </Field>
      )}
      {part && (
        <ul className="add-part-numbers" aria-label="catalog numbers">
          {part.numbers.map((n) => (
            <li key={n.key} title={`${numberName(n.key)} ${n.value} ${n.unit} · ${n.source}`}>{numberName(n.key)} {n.value} {n.unit}{n.kind === "assumed" ? " (assumed)" : ""} · {sourceName(n.source)}</li>
          ))}
        </ul>
      )}
      {catalog.rails.length > 0 && (
        <Field label="Rail" control="select" aria-label="rail" value={rail} onChange={text(setRail)} {...f("rail")}>
          <option value="">{noFit ? "Make a new rail" : "Pick a rail"}</option>
          {catalog.rails.map((r) => <option key={r} value={r}>{r}</option>)}
        </Field>
      )}
      {makeRail && (
        <div className="declare-review" aria-label="Power for this part">
          <b>Power for this part</b>
          <Field label="Rail name" aria-label="rail name" value={railName} onChange={text(setRailName)} {...f("rname")} />
          <Field label="Voltage (V)" aria-label="rail voltage" value={volt} onChange={text(setVolt)} {...f("volt")} />
          <Field label="Most it can deliver (W)" aria-label="rail capacity" value={cap} onChange={text(setCap)} {...f("cap")} />
          <small className="add-part-help">The supply this part runs from. For a battery, the most it can deliver is volts × amp-hours × C rating.</small>
        </div>
      )}
      {part && !rail && catalog.rails.length > 0 && <small className="add-part-help">{part.fits.length > 1 ? "Several rails match this part's voltage" : "No rail matches this part's voltage"} — pick one</small>}
      {canAttach && (
        <Disclosure summary="Attach to a link">
          <div className="declare-review">
            <PlaceFields links={links} parent={attachTo} setParent={setAttachTo} xyz={attXyz} setXyz={setAttXyz} none frames={part!.frames ?? []} snap={snap} setSnap={setSnap} />
            {attachError && <Notice kind="error">Type the position as numbers.</Notice>}
          </div>
        </Disclosure>
      )}
      <Button variant="primary" onClick={() => void submit()} disabled={result === "busy"}>Add part</Button>
      {result && result !== "busy" && (result.written
        ? <Notice kind="info">Part added</Notice>
        : <Notice kind="error">{result.refusal ?? "Refused"}</Notice>)}
      <Disclosure summary="Details" open={!part || undefined}>
        <div className="declare-review">
          <Field label="Part name" placeholder="e.g. dev.servo5" aria-label="part name" value={id} onChange={text(setId)} {...f("id")} />
          <Field label="Power draw (W)" aria-label="power draw" value={draw} onChange={text(setDraw)} {...f("draw")} />
          <Field label="Source" aria-label="part source" value={source} onChange={text(setSource)} {...f("source")} />
          {askWitness && <Field label="Checked by" hint="Who checked it" aria-label="part checked by" value={witness} onChange={text(setWitness)} {...f("witness")} />}
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
