import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { Button, Field, Notice, PanelFooter, Segmented } from "./kit";
import type { CheckPayload, PagePayload, RobotJoint } from "../shared/api.gen";
import type { ContractWriteRequest } from "../shared/ipc-contract";
import { isPosed, useViewState } from "./scene/viewState";
import { setSelectedPartId } from "./scene/selectionStore";
import { angleToRad, angleUnit, angleWord, lenNum, lenToMm, lenUnit, useUnits } from "./units";
import { isMoveKey, onMac, typedMoveStep, typing, type TypedMove } from "./shortcuts";

type Command = "place" | "move" | "rotate" | "reparent" | "mount-shape";
type Triple = [number, number, number];
type ShapeKind = "plate" | "bracket" | "tube" | "custom-mesh";

type Mat = number[][];
const rotX = (a: number): Mat => [[1, 0, 0], [0, Math.cos(a), -Math.sin(a)], [0, Math.sin(a), Math.cos(a)]];
const rotY = (a: number): Mat => [[Math.cos(a), 0, Math.sin(a)], [0, 1, 0], [-Math.sin(a), 0, Math.cos(a)]];
const rotZ = (a: number): Mat => [[Math.cos(a), -Math.sin(a), 0], [Math.sin(a), Math.cos(a), 0], [0, 0, 1]];
const mul = (a: Mat, b: Mat): Mat => a.map((row) => [0, 1, 2].map((j) => row[0]! * b[0]![j]! + row[1]! * b[1]![j]! + row[2]! * b[2]![j]!));
const rpyMatrix = ([r, p, y]: Triple): Mat => mul(rotZ(y), mul(rotY(p), rotX(r))); // URDF fixed-axis xyz

/** The engine SETS a joint's rpy (radians). "Rotate by" turns the part about its own axes, so the new rotation is current * typed, read back as rpy. Rolling alone just adds to roll. */
export function rotateBy(current: Triple, by: Triple): Triple {
  const m = mul(rpyMatrix(current), rpyMatrix(by));
  const p = Math.asin(Math.max(-1, Math.min(1, -m[2]![0]!)));
  const flat = Math.abs(m[2]![0]!) > 1 - 1e-9; // pitch +-90: roll and yaw are one angle, keep the yaw
  const r = flat ? Math.atan2(-m[1]![2]!, m[1]![1]!) : Math.atan2(m[2]![1]!, m[2]![2]!);
  const y = flat ? 0 : Math.atan2(m[1]![0]!, m[0]![0]!);
  return [r, p, y].map((v) => Math.round(v * 1e9) / 1e9) as Triple;
}

export type DesignEditRequest = Extract<
  ContractWriteRequest,
  { command: "place" | "move" | "rotate" | "reparent" | "mount-shape" | "delete" | "undo-edit" | "redo-edit" }
>;
/** What Delete removes: the selected device, link or joint by its engine name, and the name to show. */
export type DeleteTarget = { kind: "device" | "link"; name: string; label: string };
/** Delete/Backspace only counts when the 3D view or the Parts tree has focus. */
const inPartsOrView = (el: Element | null) => !!el && (el.tagName === "CANVAS" || !!el.closest(".kit-tree"));
/** The source every in-app edit records unless the builder types one (also what a drop or "Place on…" records). */
export const PLACED_SOURCE = "placed in the app";
export type DesignEditReply = { written: boolean; refusal: string | null; diagnostic?: string | null };

const mm = (metres: number) => lenNum(metres * 1000); // shown in the chosen length unit

/** Click a joint: where the tool tip is, and what one step of this joint does to it. Nothing when the engine had no tip. */
export function TipReadout({ joint }: { joint: RobotJoint }) {
  const tip = joint.tip;
  useUnits();
  const posed = isPosed(useViewState());
  if (!tip) return null;
  const rotary = joint.type !== "prismatic";
  const scale = rotary ? angleToRad(1) : lenToMm(1) / 1000; // per shown angle unit, or per shown length unit, in metres or radians
  const step = tip.per_unit.map((v) => v * scale);
  const moves = step.every((v) => Math.abs(v) < 1e-9) ? "does not move the tip" : `moves the tip by x ${mm(step[0]!)}, y ${mm(step[1]!)}, z ${mm(step[2]!)} ${lenUnit()}`;
  return (
    <div className="tip-readout">
      <p>Tool tip at x {mm(tip.xyz[0]!)}, y {mm(tip.xyz[1]!)}, z {mm(tip.xyz[2]!)} {lenUnit()}, {posed ? "at zero pose, not the pose you set" : "with every joint at zero"}</p>
      <p>Moving this joint 1 {rotary ? angleWord() : lenUnit()} {moves}</p>
    </div>
  );
}

export function DesignControls({
  contract,
  expectedRevision,
  selectedPartId,
  selectedPartName,
  positionMm,
  rpyRad,
  onEdit,
  onReopen,
  onPayload,
}: {
  contract: string;
  expectedRevision: string;
  selectedPartId: string | null;
  /** The name the Parts tree shows; the prompt says this, never the internal id. */
  selectedPartName?: string;
  /** Where the part sits now in the frame that holds it, in mm. The engine `move` SETS the position, so a typed distance needs this to add to. */
  positionMm?: Triple | null;
  /** The part's roll, pitch, yaw now in radians. The engine `rotate` SETS them, so a typed angle needs this to turn from. */
  rpyRad?: Triple | null;
  onEdit: (request: DesignEditRequest) => Promise<DesignEditReply>;
  onReopen: (contract: string) => Promise<CheckPayload | PagePayload>;
  onPayload: (payload: CheckPayload | PagePayload) => void;
}) {
  const [command, setCommand] = useState<Command>("move");
  const [target, setTarget] = useState(selectedPartId ?? "");
  const [parent, setParent] = useState("");
  const [typedBy, setTypedBy] = useState<Partial<Record<Command, Triple>>>({}); // Move, Rotate, Place and Mount each keep their own numbers
  const values = typedBy[command] ?? [0, 0, 0];
  const setValues = (next: Triple) => setTypedBy((t) => ({ ...t, [command]: next }));
  useUnits(); // the one Units setting (View menu): typed numbers are in it, requests go out in mm and radians
  const [snap, setSnap] = useState(false); // GAP-snap: off by default; rounds a move distance to the nearest step
  const [snapStep, setSnapStep] = useState(5); // always mm; shown in the chosen length unit
  const [kind, setKind] = useState<ShapeKind>("plate");
  const [meshPath, setMeshPath] = useState("");
  const [fastenerPattern, setFastenerPattern] = useState("");
  const [source, setSource] = useState("");
  const [addSource, setAddSource] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [diagnostic, setDiagnostic] = useState<string | null>(null);
  const [refreshPending, setRefreshPending] = useState(false);
  const [busy, setBusy] = useState(false);

  async function refresh(): Promise<void> {
    try {
      onPayload(await onReopen(contract));
      setRefreshPending(false);
      setRefusal(null);
    } catch (error) {
      setRefusal(`Write succeeded, refresh failed: ${error instanceof Error ? error.message : String(error)}. Retry refresh.`);
    }
  }

  useEffect(() => {
    if (selectedPartId) setTarget(selectedPartId);
  }, [selectedPartId]);

  async function apply(request: DesignEditRequest): Promise<string | null> {
    if (busy || refreshPending) return null;
    setBusy(true);
    try {
      const reply = await onEdit(request);
      if (!reply.written) {
        setDiagnostic(null);
        setRefusal(reply.refusal ?? "The edit was refused.");
        return reply.refusal ?? "The edit was refused.";
      }
      setRefusal(null);
      setDiagnostic(reply.diagnostic ?? null);
      setRefreshPending(true);
      await refresh();
    } catch (error) {
      setRefusal(`Write failed: ${error instanceof Error ? error.message : String(error)}. Retry applying the edit.`);
      return "The edit could not be saved.";
    } finally {
      setBusy(false);
    }
    return null;
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy || refreshPending) return; // apply would skip it and report success: keep what was typed
    if (command === "move" && !positionMm) return setRefusal("I can't tell where this part sits yet, so I can't move it by a distance.");
    if (command === "rotate" && !rpyRad) return setRefusal("I can't tell how this part is turned yet, so I can't rotate it by an angle.");
    const provenance = { source: source || PLACED_SOURCE, witness: "", expected_revision: expectedRevision };
    const request: DesignEditRequest = command === "place" ? { command, args: { ...provenance, contract, target, on: parent, offset: values.map(lenToMm) as Triple, unit: "mm" } }
      : command === "move" ? { command, args: { ...provenance, contract, target, offset: moveTo(values), unit: "mm" } }
        : command === "rotate" ? { command, args: { ...provenance, contract, target, rpy: rotateBy(rpyRad!, values.map(angleToRad) as Triple), unit: "rad" } }
          : command === "reparent" ? { command, args: { ...provenance, contract, target, to: parent } }
            : { command, args: { ...provenance, contract, target, shape: { kind, dimensions_mm: values.map(lenToMm) as Triple, ...(fastenerPattern ? { fastener_pattern: fastenerPattern } : {}), ...(meshPath ? { mesh_path: meshPath } : {}) } } };
    void apply(request).then((refused) => { if (refused === null) setValues([0, 0, 0]); }); // a second Apply is not a repeat
  }

  // The form says "Move by", so the engine (which SETS the position) gets the current position plus the typed distance; a blank axis keeps its coordinate.
  const snapMm = (d: number) => (snap ? Math.sign(d) * Math.round(Math.abs(d) / snapStep) * snapStep : d); // nearest step, not down; the engine still gets exact mm
  const moveTo = (by: Triple): Triple => positionMm!.map((v, i) => Math.round((v + snapMm(lenToMm(by[i]!))) * 1e6) / 1e6) as Triple;
  // G, an axis key, a number, Enter: one relative move of the selected part, through the same edit as the form.
  const [toast, setToast] = useState<string | null>(null); // a refused typed move says why even with "Edit placement" folded shut
  const [typed, setTyped] = useState<TypedMove | null>(null);
  const typedRef = useRef<TypedMove | null>(null);
  const setTypedNow = (next: TypedMove | null) => { typedRef.current = next; setTyped(next); };
  const keyRef = useRef<(e: KeyboardEvent) => void>(() => {});
  keyRef.current = (e) => {
    const open = typedRef.current;
    if (!open) {
      if (typing(document.activeElement)) return;
      if (isMoveKey(e) && target && !busy && !refreshPending) { e.preventDefault(); setTypedNow({ axis: null, text: "" }); }
      return;
    }
    const step = typedMoveStep(open, e);
    if (!step) return;
    e.preventDefault();
    e.stopPropagation();
    setTypedNow(step.state);
    setToast(null);
    if (!step.commit) return;
    if (!positionMm) return setToast("I can't tell where this part sits yet, so I can't move it by a distance.");
    const offset = positionMm.map((v, i) => Math.round((v + snapMm(lenToMm(step.commit![i]!))) * 1e6) / 1e6) as Triple; // engine move sets the position: current + typed
    void apply({ command: "move", args: { source: PLACED_SOURCE, witness: "", expected_revision: expectedRevision, contract, target, offset, unit: "mm" } }).then(setToast);
  };
  useEffect(() => {
    const down = (e: KeyboardEvent) => keyRef.current(e);
    window.addEventListener("keydown", down, true);
    return () => window.removeEventListener("keydown", down, true);
  }, []);

  const formId = useId();
  const dirty = !!(source || parent || meshPath || fastenerPattern || values.some((v) => v !== 0) || command !== "move" || kind !== "plate");
  const more = command === "place" || command === "reparent";
  const needsValues = command !== "reparent";
  const text = (label: string, value: string, set: (v: string) => void, required = false, placeholder?: string, hint?: string) =>
    <Field label={label} aria-label={label} placeholder={placeholder} hint={hint} value={value} onChange={(event: React.ChangeEvent<HTMLInputElement>) => set(event.target.value)} required={required} />;
  const num = (index: number, axis: string) => { const label = command === "move" ? `Move by ${axis}` : command === "rotate" ? `Rotate by ${["roll", "pitch", "yaw"][index]}` : axis; return (
    <Field label={label} aria-label={label} type="number" value={values[index]} onChange={(event: React.ChangeEvent<HTMLInputElement>) => setValues(values.map((value, i) => i === index ? Number(event.target.value) : value) as Triple)} />); };
  const label = selectedPartName ?? "the selected part";
  const prompt = (typed || toast) && createPortal(
    <div className="typed-move" role="status">
      {typed ? (typed.axis === null ? `Move ${label}: press X, Y or Z` : `Move ${label}: ${"xyz"[typed.axis]} ${typed.text || "…"} ${lenUnit()}`) : toast}
      {typed && <span>Enter to apply, Esc to cancel</span>}
    </div>, document.body);
  return (
    <form id={formId} className="design-controls" onSubmit={submit}>
      <Segmented
        label="Operation"
        value={more ? "more" : command}
        onChange={(v) => setCommand(v === "more" ? "place" : v)}
        options={[{ value: "move", label: "Move" }, { value: "rotate", label: "Rotate" }, { value: "mount-shape", label: "Mount" }, { value: "more", label: "More" }]}
      />
      {more && <Segmented label="More operations" value={command} onChange={setCommand} options={[{ value: "place", label: "Place" }, { value: "reparent", label: "Reparent" }]} />}
      {(command === "place" || command === "reparent") && text(command === "place" ? "On link" : "New parent", parent, setParent, true)}
      {needsValues && <div className="design-controls-values">{num(0, "X")}{num(1, "Y")}{num(2, "Z")}</div>}
      {needsValues && <small className="add-part-help">{command === "rotate" ? `Angles in ${angleUnit()}` : `Distances in ${lenUnit()}`}. Change it under View, Units.</small>}
      {command === "move" && <div className="design-controls-snap">
        <label><input type="checkbox" aria-label="Snap to grid" checked={snap} onChange={(e) => setSnap(e.target.checked)} /> Snap to grid</label>
        {snap && <Segmented label="Grid step" value={String(snapStep)} onChange={(v) => setSnapStep(Number(v))} options={[1, 5, 10].map((mm) => ({ value: String(mm), label: `${lenNum(mm)} ${lenUnit()}` }))} />}
      </div>}
      {command === "mount-shape" && <>
        <Field label="Primitive" aria-label="Primitive" control="select" value={kind} onChange={(event: React.ChangeEvent<HTMLSelectElement>) => setKind(event.target.value as ShapeKind)}>
          <option value="plate">Plate</option><option value="bracket">Bracket</option><option value="tube">Tube</option><option value="custom-mesh">Custom mesh</option>
        </Field>
        {text("Fastener pattern", fastenerPattern, setFastenerPattern)}
        {kind === "custom-mesh" && text("Mesh path", meshPath, setMeshPath, true)}
      </>}
      {addSource ? text("Source", source, setSource, false, "e.g. caliper, CAD file", "Where the number came from") : <Button variant="quiet" onClick={() => setAddSource(true)}>Add a source</Button>}
      {dirty && <PanelFooter><Button type="submit" form={formId} variant="primary" disabled={busy || refreshPending || !target}>Apply</Button></PanelFooter>}
      {refreshPending && <Button disabled={busy} onClick={() => void refresh()}>Retry refresh</Button>}
      {refusal && <div className="design-controls-refusal"><Notice kind="error">{refusal}</Notice></div>}
      {prompt}
      {diagnostic && <div className="design-controls-diagnostic"><Notice kind="info">{diagnostic}</Notice></div>}
    </form>
  );
}

/**
 * Delete/Backspace removes the selected part through the engine's delete route; Cmd/Ctrl+Z and +Shift+Z step the engine's
 * edit journal one edit back or forward. Mounted for the whole Design tab (not with the Edit form), so undo still works
 * when the delete left nothing selected. `target` is the current selection, set by the panel on each render.
 */
export function EditKeys({ contract, expectedRevision, target, onEdit, onReopen, onPayload }: {
  contract: string;
  expectedRevision: string;
  target: { current: DeleteTarget | null };
  onEdit: (request: DesignEditRequest) => Promise<DesignEditReply>;
  onReopen: (contract: string) => Promise<CheckPayload | PagePayload>;
  onPayload: (payload: CheckPayload | PagePayload) => void;
}) {
  const [toast, setToast] = useState<string | null>(null); // the engine's refusal as-is, or one line saying what happened
  const busy = useRef(false);
  const keyRef = useRef<(e: KeyboardEvent) => void>(() => {});
  async function run(request: DesignEditRequest, done: string, then?: () => void) {
    busy.current = true;
    setToast(null);
    try {
      const reply = await onEdit(request);
      if (!reply.written) return setToast(reply.refusal ?? "The edit was refused.");
      then?.();
      onPayload(await onReopen(contract));
      setToast(done);
    } catch (error) {
      setToast(error instanceof Error ? error.message : String(error));
    } finally {
      busy.current = false;
    }
  }
  keyRef.current = (e) => {
    if (typing(document.activeElement) || busy.current || e.defaultPrevented) return;
    const mod = (e.metaKey || e.ctrlKey) && !e.altKey;
    // APP-3: in the app the Edit menu owns Cmd+Z (its accelerator); acting here too would undo twice.
    if (mod && e.key.toLowerCase() === "z" && !window.lockwire?.onOpenedFromMenu) {
      e.preventDefault();
      const redo = e.shiftKey;
      void run({ command: redo ? "redo-edit" : "undo-edit", args: { contract } }, redo ? "Redid it." : "Undid the last edit.");
    } else if ((e.key === "Delete" || e.key === "Backspace") && !mod && !e.metaKey && !e.altKey && target.current && inPartsOrView(document.activeElement)) {
      e.preventDefault();
      const { kind, name, label } = target.current;
      void run({ command: "delete", args: { source: PLACED_SOURCE, witness: "", expected_revision: expectedRevision, contract, kind, name } }, `Deleted ${label}. ${onMac() ? "Cmd" : "Ctrl"}+Z brings it back.`, () => setSelectedPartId(null));
    }
  };
  useEffect(() => {
    const down = (e: KeyboardEvent) => keyRef.current(e);
    window.addEventListener("keydown", down);
    return () => window.removeEventListener("keydown", down);
  }, []);
  return toast ? createPortal(<div className="typed-move" role="status">{toast}</div>, document.body) : null;
}
