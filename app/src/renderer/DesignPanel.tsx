/**
 * The Design page: no canvas of its own. The Shell's RobotView is the canvas; this
 * file supplies what floats over it -- `DesignParts` (left), `DesignPanel` (right: Inspector,
 * Edit placement, Analysis), `DesignLens` + `DesignTools` (bottom) and `DesignSceneExtras`
 * (mounted inside the RobotView: callouts and the e2e pixel hook). Lens, callouts and the
 * analysis panel's open state live in `scene/designView.ts`; selection in `selectionStore.ts`.
 *
 * this file composes no lens word and re-derives no geometry. It only narrows the check
 * payload's own `scene`/`graph` fields to the shapes `Canvas` already take, and decodes
 * nothing itself — `Canvas` -> `buildScene` -> `decode.ts` still own that.
 *
 * `CheckPayload.scene` is `ScenePayload | SceneUnchanged | None` (api_payload.py) — only a real
 * `ScenePayload` (has `.parts`) has anything to draw; `SceneUnchanged` (`{unchanged: true}`) and
 * `null` both mean "nothing to show yet", same as no payload at all.
 */
import { useEffect, useRef, useState } from "react";
import * as THREE from "three";

import type { AddPartPayload, CheckPayload, PagePayload, RobotJoint, RobotLink, RobotParam, SceneLabel, ScenePart as ApiScenePart } from "../shared/api.gen";
import { useScene } from "./scene/Canvas";
import { isMovable, isPosed, useViewState } from "./scene/viewState";
import { DesignControls, EditKeys, TipReadout, type DeleteTarget, type DesignEditReply, type DesignEditRequest } from "./DesignControls";
import { CalloutLayer, type CalloutAnchor } from "./scene/CalloutLayer";
import { activeCamera, headroomText, supportPolygonObject } from "./scene/buildScene";
import { Icon } from "./kit/icons";
import { limbOf } from "./kit/limb";
import { Button, Disclosure, EmptyState, Field, IconButton, Inspector, Jumper, Notice, Panel, PartTree, PillTabs, Figure, type Status } from "./kit";
import type { OnJump } from "./jump";
import { highlightsFromCards, readThemeColors } from "./scene/robotScene";
import { LENS_IDS, type LensedPart } from "./scene/lenses";
import { pro } from "./proRegistry";
import { AddPartForm, type AddPartSubmission, type StructureSubmission } from "./AddPartForm";
import { AddShapeForm, type ShapeSubmission } from "./AddShapeForm";
import { PoseControls } from "./PoseControls";
import { angleNum, angleUnit, lenNum, lenUnit, useUnits } from "./units";
import { NamedSizes } from "./NamedSizes";
import type { DeclareSubmitResult } from "./DeclareFace";
import { isAddPartKey, typing } from "./shortcuts";
import { ConnectionLines, ConnectionsPanel } from "./Connections";
import { SwapPanel } from "./SwapPanel";
import { CONN_LENSES, type ConnLens } from "./scene/connections";
import { resetDesignView, setDesignView, useDesignView } from "./scene/designView";
import {
  readPaintedSilhouetteBounds,
  type ScenePart,
  type SceneContext,
} from "./scene/buildScene";
import { setSelectedGroup, setSelectedPartId, setSelectionSceneKey, useSelectedGroup, useSelectedPartId } from "./scene/selectionStore";
import type { PackedVectors } from "./scene/decode";

declare global {
  interface Window {
    __lockwireReadNonBackgroundPixel?: () => [number, number, number, number];
    __lockwireReadSilhouetteBounds?: () => {
      width: number;
      height: number;
      minX: number;
      maxX: number;
      minY: number;
      maxY: number;
      /** The px the floating panels cover, the same inset the camera was framed with (canvas px). */
      inset: { top: number; right: number; bottom: number; left: number };
    } | null;
  }
}

// The hidden e2e's readback hook (E2-j2), same idiom as `lens-e2e-entry.tsx`'s
// `__readCenterPixel` — but living in the REAL Canvas mount rather than a parallel test
// harness, so the pixel it reads comes from the app a person actually opens. Inert without a
// caller: nothing here fires unless a test invokes it.
let liveCtx: SceneContext | null = null;

function SceneCapture() {
  const ctx = useScene();
  useEffect(() => {
    liveCtx = ctx;
    return () => {
      liveCtx = null;
    };
  }, [ctx]);
  return null;
}

/** The payload owns label identity. Match it to the mounted mesh's payload id rather than a
 * part-array index, then project that real mesh's center through the mounted camera. */
function MountedCallouts({
  labels,
  onOverflowChange,
}: {
  labels: SceneLabel[];
  onOverflowChange: (count: number) => void;
}) {
  const ctx = useScene();
  useViewState(); // the flat view swaps the camera the labels follow
  const getAnchors = (): CalloutAnchor[] =>
    labels.flatMap((label) => {
      const mesh = ctx.meshes.find((candidate) => candidate.userData.partId === label.part);
      if (!mesh || !mesh.visible) return [];
      return [{
        id: label.part,
        position: new THREE.Box3().setFromObject(mesh).getCenter(new THREE.Vector3()),
        text: label.title,
        kind: "derived",
      }];
    });

  return (
    <CalloutLayer
      poolSize={4}
      camera={activeCamera(ctx)}
      viewportWidth={ctx.resolution.x}
      viewportHeight={ctx.resolution.y}
      getAnchors={getAnchors}
      onLabelClick={setSelectedPartId}
      onOverflowChange={onOverflowChange}
    />
  );
}

/** `PackedVectors.offset`/`.scale` are `null` on the wire when absent, and (E2-j2 rebase:
 * regenerated `api.gen.ts` via `lockwire api-types`, replacing this box's own provisional
 * hand-typed one -- rule 82) also optional now that the real schema marks them not-required;
 * `decode.ts` reads `undefined` for "absent" instead. The only translation this file does. */
function toPackedVectors(packed: {
  type: "i16";
  data: string;
  scale?: number | null;
  offset?: number[] | null;
}): PackedVectors {
  return {
    type: packed.type,
    data: packed.data,
    scale: packed.scale ?? undefined,
    offset: packed.offset ? (packed.offset as [number, number, number]) : undefined,
  };
}

export function toScenePart(part: ApiScenePart): ScenePart & LensedPart {
  return {
    id: part.id,
    positions: toPackedVectors(part.positions),
    normals: part.normals ? toPackedVectors(part.normals) : undefined,
    indices: part.indices,
    lenses: part.lenses,
  };
}

type Payload = CheckPayload | PagePayload | null;

/** One tap: the engine exports this link's STL in mm and opens it; its one line comes back here. Never talks to a printer. */
function PrintPart({ contract, link, hasShape }: { contract: string; link: string; hasShape: boolean }) {
  const [result, setResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => setResult(null), [link]);
  const press = async () => {
    setBusy(true);
    try { setResult(await window.lockwire.printPart(contract, link)); } catch (e) { setResult(e instanceof Error ? e.message : String(e)); } finally { setBusy(false); }
  };
  return (
    <div className="print-part">
      <Button variant="secondary" disabled={!hasShape || busy} onClick={() => void press()}>Print this part</Button>
      {!hasShape && <small>This part has no shape to print.</small>}
      {result && <small role="status">{result}</small>}
    </div>
  );
}

const linksOf = (payload: Payload): string[] => [...new Set([...(payload && "links" in payload ? payload.links.map((l) => l.name) : []), ...jointsOf(payload).flatMap((j) => [j.parent, j.child])])];
export const jointsOf = (payload: Payload): RobotJoint[] => (payload && "joints" in payload ? payload.joints : []);

const STATE: Record<ApiScenePart["state"], Status> = { fault: "bad", warn: "warn", affected: "muted", ok: "go", unchecked: "muted", plain: "muted" };
const UNIT = { revolute: "rad", continuous: "rad", prismatic: "m" } as Record<string, string>;

function sceneOf(payload: Payload) {
  const scene = payload?.scene;
  return scene && "parts" in scene ? scene : undefined;
}

type TreeRow = { id: string; name: string; state?: Status; depth?: number; sel?: string; sels?: string[]; group?: { id: string; label: string; kind?: string } };
const WORST: Status[] = ["bad", "warn", "go", "muted"];
const DOT_WORDS: Partial<Record<Status, string>> = { bad: "fault", warn: "warn", go: "ok", muted: "Not checked" };
const PART_STATE_WORDS: Record<string, string> = { fault: "Fails", warn: "Check", affected: "Affected", ok: "OK", unchecked: "Not checked", plain: "Not checked" };
const stateValue = (state: string | undefined) => { const st = STATE[state as ApiScenePart["state"]] ?? "muted"; const w = PART_STATE_WORDS[state ?? "unchecked"]; return <span className="design-state"><i className={`kit-dot kit-s-${st}`} aria-hidden="true" />{w}</span>; };
const KIND_WORDS: Record<string, string> = { device: "Part", rail: "Power rail", bus: "Data bus", link: "Link", other: "Structure" };

/** Parts come from the mesh scene: one row per mesh node, listed under the engine's own `group`
 * (the app never works out which component a part belongs to). A robot with no mesh
 * (QArm, ANYmal) lists its links as a tree from the root, each row picking the joint that carries
 * the link, dotted from the Check cards' own findings; each top-level chain (a leg) is a group
 * when there is more than one. */
function placedRows(payload: Payload): TreeRow[] {
  const scene = sceneOf(payload);
  if (scene && scene.parts.length > 0) {
    // Same-named parts in one group (four fan blades) fold into one row "Fan blade ×4" (selects the first).
    const folded = new Map<string, TreeRow & { n: number }>();
    for (const part of scene.parts) {
      const key = `${part.group.id}\n${part.name}`;
      const seen = folded.get(key);
      if (seen) {
        seen.n += 1;
        seen.sels!.push(part.id ?? part.node);
        if (WORST.indexOf(STATE[part.state]) < WORST.indexOf(seen.state!)) seen.state = STATE[part.state];
      }
      else folded.set(key, { id: part.node, sel: part.id ?? part.node, sels: [part.id ?? part.node], name: part.name, state: STATE[part.state], group: part.group, n: 1 });
    }
    const rows = [...folded.values()].map(({ n, ...row }) => (n > 1 ? { ...row, name: `${row.name} ×${n}` } : row));
    // A robot of many one-part links (ANYmal) gets Legs / Shells / Cameras drop-downs instead of 80 flat rows.
    const solo = (r: TreeRow) => r.group && rows.filter((o) => o.group?.id === r.group!.id).length === 1;
    if (rows.filter(solo).length < 8) return rows;
    const bucket = (name: string) => /camera/i.test(name) ? "Cameras" : /\b(hip|thigh|shank|foot|knee)\b/i.test(name) ? "Legs" : /shell|face|body/i.test(name) ? "Shells" : null;
    return rows.map((r) => {
      if (!solo(r)) return r;
      const l = limbOf(r.name);
      if (l) return { ...r, name: l.rest, group: { id: `limb:${l.label}`, label: l.label, kind: r.group!.kind } };
      const b = bucket(r.name);
      return b ? { ...r, group: { id: `bucket:${b}`, label: b, kind: r.group!.kind } } : r;
    });
  }
  const joints = jointsOf(payload);
  const level = new Map(highlightsFromCards(payload && "cards" in payload ? payload.cards : [], joints).map((h) => [h.id, h.level]));
  const out: TreeRow[] = [];
  const walk = (link: string, depth: number, seen: Set<string>, group?: { id: string; label: string }) => {
    for (const joint of joints.filter((j) => j.parent === link && !seen.has(j.name))) {
      const state: Status = level.get(joint.name) === "bad" ? "bad" : level.get(joint.name) === "warn" ? "warn" : "go";
      const g = group ?? { id: joint.name, label: joint.child };
      out.push({ id: joint.name, name: joint.child_name, state, depth, group: g });
      walk(joint.child, depth + 1, new Set(seen).add(joint.name), g);
    }
  };
  const children = new Set(joints.map((j) => j.child));
  for (const root of new Set(joints.map((j) => j.parent).filter((p) => !children.has(p)))) walk(root, 0, new Set());
  // one chain is not worth a drop-down
  return new Set(out.map((r) => r.group?.id)).size > 1 ? out : out.map(({ group: _g, ...row }) => row);
}

const UNPLACED = { id: "unplaced", label: "Not placed yet", kind: "unplaced", open: true };
/** Devices the robot declares that have no body in the scene yet (a part just added, or every device when there is no scene): the page's own device list minus the scene's parts. */
export function unplacedOf(payload: Payload): { id: string; name: string }[] {
  const drawn = new Set(sceneOf(payload)?.parts.map((p) => p.id ?? p.node));
  const devices = payload && "faces" in payload ? payload.faces.reference.devices : [];
  return devices.filter((d) => !drawn.has(d.id));
}
/** The root part (Chassis, Base): the link no joint carries. A mesh robot may already draw it as a scene part, then it is listed there. */
export function rootLinksOf(payload: Payload): { id: string; name: string }[] {
  const joints = jointsOf(payload);
  const children = new Set(joints.map((j) => j.child));
  const drawn = new Set(sceneOf(payload)?.parts.flatMap((p) => [p.id ?? p.node, p.node]));
  return [...new Map(joints.filter((j) => !children.has(j.parent) && !drawn.has(j.parent)).map((j) => [j.parent, { id: j.parent, name: j.parent_name }])).values()];
}
const rowsOf = (payload: Payload): TreeRow[] => [
  ...rootLinksOf(payload).map((r) => ({ id: r.id, sel: r.id, sels: [r.id], name: r.name, state: "muted" as const })),
  ...placedRows(payload),
  ...unplacedOf(payload).map((d) => ({ id: d.id, sel: d.id, sels: [d.id], name: d.name, state: "muted" as const, group: UNPLACED })),
];

/** LEFT: the part tree. Two-way with the 3D pick through the selection store; a group header
 * selects every part under it. */
export function DesignParts({ payload, add }: { payload: Payload; add?: { witness: string; catalog?: AddPartPayload; onAdd: (part: AddPartSubmission) => Promise<DeclareSubmitResult>; onAddStructure?: (s: StructureSubmission) => Promise<DeclareSubmitResult>; shape?: { links: RobotLink[]; params: RobotParam[]; onAdd: (shape: ShapeSubmission) => Promise<DeclareSubmitResult>; onSize: (name: string, value: number) => Promise<DeclareSubmitResult> } } }) {
  const [adding, setAdding] = useState(false);
  const [addingShape, setAddingShape] = useState(false);
  const [sizing, setSizing] = useState(false);
  // Shift+A with the 3D view or this panel focused, never while typing.
  useEffect(() => {
    if (!add) return;
    const down = (e: KeyboardEvent) => {
      const el = document.activeElement;
      if (!isAddPartKey(e) || typing(el) || !(el?.tagName === "CANVAS" || el?.closest(".design-parts"))) return;
      e.preventDefault();
      setAdding(true);
    };
    window.addEventListener("keydown", down);
    return () => window.removeEventListener("keydown", down);
  }, [add]);
  // Escape closes an open Add form without writing anything.
  useEffect(() => {
    if (!adding && !addingShape) return;
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape" && !e.defaultPrevented) { setAdding(false); setAddingShape(false); } };
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [adding, addingShape]);
  const selected = useSelectedPartId();
  const group = useSelectedGroup();
  const { lens } = useDesignView();
  // Rails and wires belong to the Power / Data lens (the connection list), not the part tree.
  const rows = rowsOf(payload).filter((r) => !payload?.graph || (r.group?.kind !== "rail" && r.group?.kind !== "bus"));
  if (payload?.graph && CONN_LENSES.includes(lens)) {
    const drawn = new Set(sceneOf(payload)?.parts.map((p) => p.id ?? p.node));
    return <ConnectionsPanel payload={payload} lens={lens as ConnLens} drawn={drawn} />;
  }
  if (rows.length === 0 && !add) return null;
  const words = WORST.filter((st) => rows.some((r) => r.state === st));
  const form = add && (adding
    ? (
      <>
        <AddPartForm askWitness={false} witness={add.witness} catalog={add.catalog} onAdd={async (part) => { const done = await add.onAdd(part); if (done.written) setAdding(false); return done; }} links={add.shape?.links.map((l) => l.name)} materials={payload && "materials" in payload ? payload.materials ?? [] : []} onAddStructure={add.onAddStructure && (async (s) => { const done = await add.onAddStructure!(s); if (done.written) setAdding(false); return done; })} />
        <Button variant="quiet" onClick={() => setAdding(false)}>Cancel</Button>
      </>
    )
    : addingShape && add.shape
      ? (
        <>
          <AddShapeForm witness={add.witness} links={add.shape.links.map((l) => l.name)} params={add.shape.params} onAdd={async (shape) => { const done = await add.shape!.onAdd(shape); if (done.written) setAddingShape(false); return done; }} />
          <Button variant="quiet" onClick={() => setAddingShape(false)}>Cancel</Button>
        </>
      )
      : sizing && add.shape
        ? (
          <>
            <NamedSizes params={add.shape.params} links={add.shape.links} onSave={add.shape.onSize} />
            <Button variant="quiet" onClick={() => setSizing(false)}>Close</Button>
          </>
        )
        : (
          <>
            <Button variant="primary" onClick={() => setAdding(true)}>Add part</Button>
            {add.shape && <Button variant="quiet" onClick={() => setAddingShape(true)}>Add shape</Button>}
            {add.shape && add.shape.params.length > 0 && <Button variant="quiet" onClick={() => setSizing(true)}>Named sizes</Button>}
          </>
        ));
  if (rows.length === 0) return <Panel title="Parts"><div className="design-parts"><p>No parts yet. Add one below.</p>{form}</div></Panel>;
  return (
    <Panel title="Parts">
      <div className="design-parts">
      {form}
      {!adding && <PartTree parts={rows} selected={selected ?? undefined} onSelect={setSelectedPartId} selectedGroup={group?.id} onSelectGroup={(id, members) => setSelectedGroup({ id, members })} />}
      <p className="design-legend" aria-label="Dot legend">
        {words.map((st) => <span key={st}><i className={`kit-dot kit-s-${st}`} aria-hidden="true" />{DOT_WORDS[st]}</span>)}
      </p>
      </div>
    </Panel>
  );
}

/** BOTTOM, above the tool pill: lens tabs (payload graph words) and the callout-overflow note. */
export function DesignLens({ payload }: { payload: Payload }) {
  const { lens, overflow } = useDesignView();
  const graph = payload?.graph;
  return (
    <>
      {overflow > 0 && (
        <Notice kind="info">
          {`${overflow} additional callout${overflow === 1 ? "" : "s"} exceed${overflow === 1 ? "s" : ""} the scene label limit.`}
        </Notice>
      )}
      {graph && (
        <div className="design-lens" title={graph[lens].caption}>
          <PillTabs
            label="Lens"
            value={lens}
            onChange={(id) => setDesignView({ lens: id, hover: null })}
            tabs={LENS_IDS.filter((id) => pro || id !== "health").map((id) => ({ id, label: id === "off" ? "All" : graph[id].label }))}
          />
        </div>
      )}
    </>
  );
}

type Row = { label: string; value: React.ReactNode };
// ---- Mass, centre of mass, inertia (the forces payload) and a joint's placement readout.
type ForceFig = NonNullable<CheckPayload["forces"]>["cg"];
const fixed = (v: number, d: number) => String(Number(v.toFixed(d)));
const nameOfMassRow = (id: string) => id.split("/")[id.startsWith("devices/") ? 1 : 2] ?? id;
/** Parts that declare no mass: said in plain words, never summed as 0 kg. */
export function massGaps(payload: Payload): string[] {
  const forces = payload && "forces" in payload ? payload.forces : undefined;
  return (forces?.masses ?? []).filter((m) => m.kind === "not_scored").map((m) => nameOfMassRow(m.id ?? ""));
}
type Material = NonNullable<PagePayload["materials"]>[number];
type Compare = NonNullable<PagePayload["material_compare"]>[number];
const signed = (v: number, d: number) => `${v > 0 ? "+" : ""}${Number(v.toFixed(d))}`;
/** CADN-matpanel: the engine's per-material rows for one link; every figure is from the payload, nothing is worked out here. */
function CompareMaterials({ compare, page, onPick }: { compare: Compare; page?: PagePayload; onPick?: (name: string) => void }) {
  if (compare.rows.length === 0) return <small className="add-part-help">Declared, no volume to compare</small>;
  const now = compare.rows.find((r) => r.current);
  return (
    <Disclosure summary="Compare materials">
      <table className="material-compare">
        <thead><tr><th>Material</th><th>{compare.link} mass</th><th>{now ? "Robot mass change" : "Robot mass"}</th><th>{now ? "Worst torque margin change" : "Worst torque margin"}</th></tr></thead>
        <tbody>
          {compare.rows.map((r) => (
            <tr key={r.material} aria-current={r.current ? "true" : undefined} className={r.current ? "current" : undefined}>
              <td>
                <button type="button" aria-label={`Use ${r.material}`} disabled={!onPick || r.current} onClick={() => onPick?.(r.material)}>{r.material}</button>
                {r.current && " (current)"}{r.printed && " (solid infill)"}
              </td>
              <td>{`${Number(r.link_mass_kg.toPrecision(3))} kg`}</td>
              <td>{r.delta_mass_kg != null ? `${signed(r.delta_mass_kg, 3)} kg` : `${Number(r.total_mass_kg.toPrecision(4))} kg`}</td>
              <td>{r.worst_margin_nm == null ? "none" : r.delta_margin_nm != null ? `${signed(r.delta_margin_nm, 2)} Nm` : `${Number(r.worst_margin_nm.toFixed(2))} Nm`}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {page && pro?.MaterialCompareMore && <pro.MaterialCompareMore page={page} link={compare.link} />}
      {!now && <small className="add-part-help">This link's material is not in the material list, so these are totals, not changes.</small>}
      {compare.note && <small className="add-part-help">{compare.note}</small>}
    </Disclosure>
  );
}
/** A catalog source reads "<link> (<who published it>: ...)"; the marker's density source is shown as just who published it. */
const sourceWords = (src: string) => /^https?:\S+ \((.*?)(?: \((?!\d)|:|$)/.exec(src)?.[1] ?? src;
/** The derived-mass marker the engine sent, with its long density source cut to who published it. */
const markerWords = (note: string) => note.replace(/\(density source: (.*)\)$/, (_, src: string) => `(density source: ${sourceWords(src)})`);
const OWN = "__own__";
const MASS_KIND_WORDS: Record<string, string> = { measured: "weighed", calculated: "from the slicer", estimated: "estimated" };
type MassEdit = { contract: string; expectedRevision: string; onEdit: (r: DesignEditRequest) => Promise<DesignEditReply>; onReopen?: (c: string) => Promise<CheckPayload | PagePayload>; onPayload?: (p: CheckPayload | PagePayload) => void };
function MassParts({ masses, edit, materials = [], compares = [], page }: { page?: PagePayload; masses: NonNullable<CheckPayload["forces"]>["masses"]; edit?: MassEdit; materials?: Material[]; compares?: Compare[] }) {
  const [refusal, setRefusal] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [own, setOwn] = useState<{ id: string; name: string; density: string; source: string } | null>(null);
  async function write(id: string, value: { name: string; density_kg_m3: number; source: string }) {
    if (!edit) return;
    // `declare` is the same write door the Declare face uses; DesignEditRequest names only the placement commands.
    const reply = await edit.onEdit({ command: "declare", args: { contract: edit.contract, path: id.replace(/\/mass$/, "/material"), value, source: "picked in the app", witness: "", expected_revision: edit.expectedRevision } } as unknown as DesignEditRequest);
    if (!reply.written) return setSaved(null), setRefusal(reply.refusal ?? "The change was refused.");
    setRefusal(null);
    setOwn(null);
    setSaved(id);
    if (edit.onReopen && edit.onPayload) edit.onPayload(await edit.onReopen(edit.contract));
  }
  function pick(id: string, name: string) {
    if (name === OWN) return setOwn({ id, name: "", density: "", source: "" });
    const m = materials.find((x) => x.name === name);
    if (m) void write(id, { name: m.name, density_kg_m3: m.density_kg_m3, source: m.source });
  }
  const density = Number(own?.density);
  const ownReady = !!own && own.name.trim() !== "" && own.source.trim() !== "" && own.density.trim() !== "" && density > 0;
  const rows = masses.filter((m) => typeof m.value === "number");
  if (rows.length === 0) return null;
  return (
    <div className="mass-parts" style={{ marginTop: 8 }}>
      {rows.map((m) => {
        const isLink = (m.id ?? "").startsWith("model/links/");
        const o = own?.id === m.id ? own : null;
        return (
          <div key={m.id} className="mass-part" style={{ marginTop: 10 }}>
            <span>{`${nameOfMassRow(m.id ?? "")}: ${Number((m.value as number).toPrecision(4))} kg, ${m.kind === "derived" && m.mass_note ? markerWords(m.mass_note) : MASS_KIND_WORDS[m.mass_kind ?? "estimated"] ?? m.mass_kind}`}</span>
            {isLink && edit && (
              <Field label="Material" aria-label={`Material for ${nameOfMassRow(m.id ?? "")}`} control="select" value={m.material ?? ""} onChange={(e: { target: { value: string } }) => pick(m.id!, e.target.value)}>
                <option value="" disabled>Pick a material</option>
                {m.material && !materials.some((x) => x.name === m.material) && <option value={m.material}>{m.material}</option>}
                {materials.map((x) => <option key={x.name} value={x.name}>{`${x.name} (${x.density_kg_m3} kg/m³)`}</option>)}
                <option value={OWN}>My own material...</option>
              </Field>
            )}
            {isLink && edit && materials.find((x) => x.name === m.material)?.caveat && <small className="add-part-help">{materials.find((x) => x.name === m.material)!.caveat}</small>}
            {isLink && edit && compares.find((c) => c.link === nameOfMassRow(m.id ?? "")) && <CompareMaterials compare={compares.find((c) => c.link === nameOfMassRow(m.id ?? ""))!} page={page} onPick={(name) => pick(m.id!, name)} />}
            {isLink && edit && o && (
              <div className="own-material">
                <Field label="Material name" aria-label="own material name" value={o.name} onChange={(e: { target: { value: string } }) => setOwn({ ...o, name: e.target.value })} />
                <Field label="Density (kg/m³)" aria-label="own material density" inputMode="decimal" value={o.density} onChange={(e: { target: { value: string } }) => setOwn({ ...o, density: e.target.value })} />
                <Field label="Where the density comes from" aria-label="own material source" placeholder="maker's data sheet or standard" value={o.source} onChange={(e: { target: { value: string } }) => setOwn({ ...o, source: e.target.value })} error={o.source.trim() === "" && o.name !== "" ? "A density needs a source." : undefined} />
                <Button disabled={!ownReady} onClick={() => void write(o.id, { name: o.name.trim(), density_kg_m3: density, source: o.source.trim() })}>Save material</Button>
              </div>
            )}
            {saved === m.id && <p>Saved. The mass stays as declared until you weigh it or import a slicer mass.</p>}
          </div>
        );
      })}
      {edit && materials.length > 0 && (
        <Disclosure summary="Where each density comes from">
          {materials.map((x) => (
            <p key={x.name} className="material-source">
              {`${x.name}, ${x.density_kg_m3} kg/m³. Source: ${x.source_label}`}
              {x.source_url && <>{" "}<a href={x.source_url} target="_blank" rel="noreferrer">link</a></>}
              {x.caveat && ` ${x.caveat}`}
            </p>
          ))}
        </Disclosure>
      )}
      {refusal && <Notice kind="error">{refusal}</Notice>}
    </div>
  );
}
export function MassReadout({ payload, edit }: { payload: Payload; edit?: MassEdit }) {
  useUnits();
  const forces = payload && "forces" in payload ? payload.forces : undefined;
  if (!forces) return null;
  const { mass_total: total, cg, inertia } = forces;
  const gaps = massGaps(payload);
  const have = (f: ForceFig): f is ForceFig & { value: number[] } => Array.isArray(f.value);
  const rows: Row[] = [];
  if (typeof total.value === "number") rows.push({ label: "Total mass", value: <Figure value={Number(total.value.toPrecision(4))} unit="kg" kind="derived" /> });
  if (have(cg)) rows.push({ label: "Centre of mass", value: `x ${lenNum(cg.value[0]! * 1000)}, y ${lenNum(cg.value[1]! * 1000)}, z ${lenNum(cg.value[2]! * 1000)} ${lenUnit()}${cg.frame ? ` in ${cg.frame}` : ""}` });
  if (inertia && have(inertia)) {
    const [xx, yy, zz, xy, xz, yz] = inertia.value as [number, number, number, number, number, number];
    rows.push({ label: "Inertia", value: `Ixx ${xx.toPrecision(3)}, Iyy ${yy.toPrecision(3)}, Izz ${zz.toPrecision(3)} kg·m² about the centre of mass` });
    if ([xy, xz, yz].some((v) => Math.abs(v) > 1e-9)) rows.push({ label: "Cross terms", value: `Ixy ${xy.toPrecision(3)}, Ixz ${xz.toPrecision(3)}, Iyz ${yz.toPrecision(3)} kg·m²` });
  }
  if (rows.length === 0 && gaps.length === 0) return null;
  const summary = typeof total.value === "number" ? `Robot mass: ${Number(total.value.toPrecision(4))} kg` : "Robot mass";
  return (
    <Panel title="Mass and balance">
      <Disclosure summary={summary}>
        {rows.length > 0 && <Inspector rows={rows} />}
        <MassParts page={payload && "material_compare" in payload ? (payload as PagePayload) : undefined} masses={forces.masses ?? []} edit={edit} materials={payload && "materials" in payload ? payload.materials ?? [] : []} compares={payload && "material_compare" in payload ? payload.material_compare ?? [] : []} />
        {gaps.length > 0 && <Notice kind="info">{`No mass declared for ${gaps.join(", ")}. The total leaves ${gaps.length === 1 ? "it" : "them"} out, it does not count ${gaps.length === 1 ? "it" : "them"} as 0 kg.`}</Notice>}
        {have(cg) && cg.note && <p>{`Centre of mass: ${cg.note}.`}</p>}
        {!have(cg) && <p>{cg.not_scored_because ?? "No centre of mass yet."}</p>}
      </Disclosure>
    </Panel>
  );
}
/** Roll, pitch, yaw (in the chosen angle unit) and the same rotation as a quaternion, from a URDF fixed-axis xyz rpy in radians. */
export function placementWords(rpy: number[]): { rpy: string; quaternion: string } {
  const [r = 0, p = 0, y = 0] = rpy;
  const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(r, p, y, "ZYX")); // three's ZYX = R = Rz*Ry*Rx = URDF
  const deg = (v: number) => (angleUnit() === "deg" ? `${angleNum(v)}°` : `${angleNum(v)} rad`);
  return { rpy: `roll ${deg(r)}, pitch ${deg(p)}, yaw ${deg(y)}`, quaternion: `x ${fixed(q.x, 3)}, y ${fixed(q.y, 3)}, z ${fixed(q.z, 3)}, w ${fixed(q.w, 3)}` };
}
/** The robot's centre of mass as a small themed sphere inside the scene (Design only, because only Design mounts this). */
function CentreOfMassMarker({ payload }: { payload: Payload }) {
  const ctx = useScene();
  const view = useViewState();
  const cg = view.com && !isPosed(view) && // the marker is the zero-pose centre of mass: hidden while posed
     payload && "forces" in payload && payload.forces?.cg.complete ? payload.forces.cg.value : null;
  const at = Array.isArray(cg) ? (cg as number[]).join(",") : "";
  useEffect(() => {
    if (!ctx.scene || !at) return;
    const box = new THREE.Box3();
    for (const m of ctx.meshes ?? []) box.union(new THREE.Box3().setFromObject(m));
    const size = box.isEmpty() ? 0.1 : Math.max(...box.getSize(new THREE.Vector3()).toArray());
    const color = readThemeColors()?.accent ?? new THREE.Color("#1560cc");
    const marker = new THREE.Mesh(new THREE.SphereGeometry(size * 0.02, 20, 12), new THREE.MeshBasicMaterial({ color, depthTest: false, depthWrite: false }));
    marker.position.set(...(at.split(",").map(Number) as [number, number, number]));
    marker.renderOrder = 11;
    marker.raycast = () => {}; // never picked
    marker.userData.decor = true; // not the robot: the pixel readers skip it
    marker.name = "centre-of-mass";
    ctx.scene.add(marker);
    ctx.needsRender = true;
    return () => {
      ctx.scene.remove(marker);
      marker.geometry.dispose();
      (marker.material as THREE.Material).dispose();
      ctx.needsRender = true;
    };
  }, [ctx, at]);
  return null;
}

/** The support polygon the engine found, flat on the ground plane (Design only, and only when the payload has one). */
function SupportPolygonMarker({ payload }: { payload: Payload }) {
  const ctx = useScene();
  const poly = payload && "support_polygon" in payload ? payload.support_polygon : null;
  const key = poly ? `${poly.z}:${poly.points.join(";")}` : "";
  useEffect(() => {
    if (!ctx.scene || !poly) return;
    const group = supportPolygonObject(poly, readThemeColors()?.accent ?? new THREE.Color("#1560cc"));
    ctx.scene.add(group);
    ctx.needsRender = true;
    return () => {
      ctx.scene.remove(group);
      group.traverse((o) => { const m = o as THREE.Mesh; m.geometry?.dispose(); (m.material as THREE.Material | undefined)?.dispose(); });
      ctx.needsRender = true;
    };
  }, [ctx, key]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

/** The torque-to-spare number beside each coloured joint, tightest first (the colour is the same ratio, so it is never the only signal). */
function HeadroomCallouts({ payload }: { payload: Payload }) {
  const ctx = useScene();
  useViewState();
  const rows = jointsOf(payload).filter((j) => j.headroom).sort((a, b) => a.headroom!.ratio - b.headroom!.ratio);
  const getAnchors = (): CalloutAnchor[] =>
    rows.flatMap((j) => {
      const mesh = ctx.meshes.find((m) => m.userData.partId === j.name && !m.userData.bone && !m.userData.ring);
      if (!mesh || !mesh.visible) return [];
      return [{ id: j.name, position: new THREE.Box3().setFromObject(mesh).getCenter(new THREE.Vector3()), text: headroomText(j.name, j.headroom!.ratio), kind: "derived" }];
    });
  if (!rows.length) return null;
  // ponytail: 12 callouts, tightest first; a bigger robot drops the roomiest joints' numbers
  return <CalloutLayer poolSize={12} camera={activeCamera(ctx)} viewportWidth={ctx.resolution.x} viewportHeight={ctx.resolution.y} getAnchors={getAnchors} onLabelClick={setSelectedPartId} />;
}

/** BOTTOM tool pill: callouts (only when the payload has some) and the analysis panel. */
export function DesignTools({ payload, canAnalyse }: { payload: Payload; canAnalyse: boolean }) {
  const view = useDesignView();
  const labels = sceneOf(payload)?.labels.length ?? 0;
  return (
    <>
      <IconButton
        icon="labels"
        label="Callouts"
        disabled={labels === 0}
        {...(labels === 0 && { title: "No callouts on this robot", "aria-label": "No callouts on this robot" })}
        pressed={view.callouts}
        onClick={() => setDesignView({ callouts: !view.callouts, overflow: 0 })}
      />
      {pro?.DesignTools && <pro.DesignTools canAnalyse={canAnalyse} />}
    </>
  );
}

/** Mounted inside the Shell's RobotView: the e2e readback hook and, when on, the callouts. */
export function DesignSceneExtras({ payload }: { payload: Payload }) {
  const { callouts, lens } = useDesignView();
  const scene = sceneOf(payload);
  const posed = isPosed(useViewState()); // the polygon and torque numbers are zero-pose figures: not shown on a posed robot
  return (
    <>
      <SceneCapture />
      <CentreOfMassMarker payload={payload} />
      {!posed && <SupportPolygonMarker payload={payload} />}
      {!posed && <HeadroomCallouts payload={payload} />}
      {CONN_LENSES.includes(lens) && <ConnectionLines payload={payload} lens={lens as ConnLens} />}
      {callouts && scene && <MountedCallouts labels={scene.labels} onOverflowChange={(overflow) => setDesignView({ overflow })} />}
    </>
  );
}

/** RIGHT: the Inspector for the selected part, "Edit placement", and the analysis panel. */
export function DesignPanel(props: DesignPanelProps) {
  const target = useRef<DeleteTarget | null>(null);
  target.current = null; // the panel below sets it once it knows what is selected
  const { contract, expectedRevision, onEdit, onReopen, onPayload } = props;
  return (
    <>
      {contract && expectedRevision && onEdit && onReopen && onPayload && <EditKeys contract={contract} expectedRevision={expectedRevision} target={target} onEdit={onEdit} onReopen={onReopen} onPayload={onPayload} />}
      <DesignPanelBody {...props} deleteTargetRef={target} />
    </>
  );
}

type DesignPanelProps = {
  payload: Payload;
  contract?: string;
  expectedRevision?: string;
  onEdit?: (request: DesignEditRequest) => Promise<DesignEditReply>;
  onReopen?: (contract: string) => Promise<CheckPayload | PagePayload>;
  onPayload?: (payload: CheckPayload | PagePayload) => void;
  /** State, Why and Findings open the first Check card about the selection. */
  onJump?: OnJump;
  /** Keyboard route for dragging a part onto the body: mount the device at a frame's origin. */
  onPlace?: (device: string, frame: string) => Promise<string | null>;
  /** A Part Swap was written from a rotary joint: re-check. */
  onSwapped?: (lines: string[]) => void;
};

function DesignPanelBody({
  deleteTargetRef,
  payload,
  contract,
  expectedRevision,
  onEdit,
  onReopen,
  onPayload,
  onJump,
  onPlace,
  onSwapped,
}: DesignPanelProps & { deleteTargetRef: { current: DeleteTarget | null } }) {
  const [placeRefusal, setPlaceRefusal] = useState<string | null>(null);
  const [swapJoint, setSwapJoint] = useState<string | null>(null);
  useUnits();
  const selectedPartId = useSelectedPartId();
  const selectedGroup = useSelectedGroup();
  const view = useDesignView();
  const scenePayload = sceneOf(payload);
  // Same key as App's: a new robot or asset drops the selection; leaving the tab keeps it.
  const sceneKey = `${contract ?? "preview"}:${scenePayload?.hash ?? "skeleton"}`;
  const rows = rowsOf(payload);
  const assetIdentity = scenePayload?.expected_asset_sha256
    ? `Selected asset ${scenePayload.asset_path ?? scenePayload.source} matches the declared model identity ${scenePayload.expected_asset_sha256}.`
    : `Selected scene ${scenePayload?.asset_path ?? scenePayload?.source}; declared model identity is unavailable.`;

  useEffect(() => setSelectionSceneKey(sceneKey), [sceneKey]);
  useEffect(() => resetDesignView, []);

  useEffect(() => {
    window.__lockwireReadNonBackgroundPixel = () => {
      if (!liveCtx) throw new Error("scene not mounted");
      const gl = liveCtx.renderer.getContext();
      const { width, height } = gl.canvas as HTMLCanvasElement;
      // A real model's geometry (unlike a bite's synthetic full-frame triangle) is not
      // guaranteed to cover the exact center pixel -- OrbitControls' default framing plus 148
      // discrete X500 parts can leave the center pixel between parts. Scanning the whole
      // framebuffer for ANY non-background pixel still proves the app drew the robot somewhere
      // in frame, which is what a person opening the Design tab actually sees.
      // The clear colour is the themed canvas, so "background" is pixel (0,0); the ground grid
      // is hidden for the scan (it is not the robot). Nothing found reports black.
      const decor = liveCtx.scene.children.filter((o) => o.userData.decor);
      decor.forEach((o) => (o.visible = false));
      liveCtx.renderer.render(liveCtx.scene, liveCtx.camera);
      const row = new Uint8Array(width * 4);
      const ref = new Uint8Array(4);
      gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, ref);
      try {
        for (let y = 0; y < height; y++) {
          gl.readPixels(0, y, width, 1, gl.RGBA, gl.UNSIGNED_BYTE, row);
          for (let x = 0; x < width; x++) {
            const i = x * 4;
            if (row[i] !== ref[0] || row[i + 1] !== ref[1] || row[i + 2] !== ref[2]) {
              return [row[i]!, row[i + 1]!, row[i + 2]!, row[i + 3]!] as [number, number, number, number];
            }
          }
        }
      } finally {
        decor.forEach((o) => (o.visible = true));
        liveCtx.needsRender = true; // the scan left a grid-less frame on screen
      }
      return [0, 0, 0, 255];
    };
    window.__lockwireReadSilhouetteBounds = () => {
      if (!liveCtx) throw new Error("scene not mounted");
      const b = readPaintedSilhouetteBounds(liveCtx.renderer, liveCtx.scene, liveCtx.camera);
      liveCtx.needsRender = true;
      if (!b) return null;
      const i = liveCtx.inset ?? { top: 0, right: 0, bottom: 0, left: 0 };
      const k = b.width / liveCtx.resolution.x; // inset is CSS px, bounds are canvas px
      return { ...b, inset: { top: i.top * k, right: i.right * k, bottom: i.bottom * k, left: i.left * k } };
    };
    return () => {
      delete window.__lockwireReadNonBackgroundPixel;
      delete window.__lockwireReadSilhouetteBounds;
    };
  }, []);

  const analysis = view.analysis && contract && expectedRevision && pro?.DesignAnalysis
    ? <pro.DesignAnalysis key={`${contract}:${expectedRevision}`} contract={contract} expectedRevision={expectedRevision} />
    : null;
  if (rows.length === 0) {
    return (
      <>
        <Panel title="Design"><div className="design-face-empty"><EmptyState title="Add a part to start." /></div></Panel>
        {analysis}
      </>
    );
  }

  const groupRows = selectedGroup ? rows.filter((r) => r.group?.id === selectedGroup.id) : [];
  const groupParts = selectedGroup ? scenePayload?.parts.filter((p) => groupRows.some((r) => r.sels?.includes(p.id ?? p.node))) ?? [] : [];
  const groupHead = groupRows[0]?.group ?? groupParts[0]?.group; // the list's own group label (limb groups included)
  const unplaced = unplacedOf(payload).find((d) => d.id === selectedPartId);
  // A device with no body yet gets a bodiless part, so its Inspector (and Place on...) work like any device's.
  const part = scenePayload?.parts.find((candidate) => (candidate.id ?? candidate.node) === selectedPartId)
    ?? (unplaced && { id: unplaced.id, node: unplaced.id, name: unplaced.name, state: "unchecked", positions: { type: "i16", data: "" }, normals: null, indices: { type: "u16", data: "" }, color: [], translation: [], lenses: [], group: { id: unplaced.id, label: unplaced.name, kind: "device" } } as ApiScenePart);
  // The inspector is titled with the name its list row shows (limb grouping may shorten a part's own name).
  const listRow = rows.find((r) => r.sels?.includes(selectedPartId ?? ""));
  const listBase = listRow?.name.replace(/ ×\d+$/, "");
  const listName = listRow && listBase ? (listRow.group?.id.startsWith("limb:") ? `${listRow.group.label} · ${listBase}` : listBase) : part?.name;
  const joint = part ? undefined : jointsOf(payload).find((candidate) => candidate.name === selectedPartId);
  const carrier = joint ? undefined : part && jointsOf(payload).find((j) => j.child === part.node); // a mesh robot's part is the link its joint moves
  const isRoot = !part && !joint && !!selectedPartId && rootLinksOf(payload).some((r) => r.id === selectedPartId); // listed, never deleted or moved from here
  const unit = joint ? UNIT[joint.type] : undefined;
  const limit = (value: number | null) => value === null ? "none" : <Figure value={Number(value.toPrecision(4))} unit={unit} kind="declared" />;
  const partLenses = part?.lenses.filter((l) => l !== "off").map((l) => l.charAt(0).toUpperCase() + l.slice(1)).join(", ");
  const worstOf = (states: string[]) => (["fault", "warn", "affected", "ok", "unchecked", "plain"] as const).find((st) => states.includes(st));
  // The first Check card that lights any of these ids: State, Why and Findings lead to it.
  const cards = payload && "cards" in payload ? payload.cards : [];
  const ids = groupParts.length ? groupParts.map((p) => p.id ?? p.node) : selectedPartId ? [selectedPartId] : [];
  const card = cards.findIndex((c) => highlightsFromCards([c], jointsOf(payload)).some((h) => ids.includes(h.id)));
  const toCard = (node: React.ReactNode) => (onJump && card >= 0 ? <Jumper label="Open its finding in Check" onClick={() => onJump({ findingIdx: card })}>{node}</Jumper> : node);
  // Findings read Check's own cards (same targets that light the robot there), never the callout labels.
  const hit = cards.filter((c) => (c.severity === "violation" || c.severity === "warning") && highlightsFromCards([c], jointsOf(payload)).some((h) => ids.includes(h.id)));
  const findingsOf = hit.length === 0 ? "No findings" : toCard(<><i className={`kit-mark kit-s-${hit.some((c) => c.severity === "violation") ? "bad" : "warn"}`} aria-hidden="true"><Icon name={hit.some((c) => c.severity === "violation") ? "fail" : "alert"} /></i>{hit.length} · {hit[0]!.title}</>);
  const lensWords = part?.lenses.filter((l) => l !== "off") ?? [];
  // A mesh named like the frame a device mounts on (the humanoid torso under its Jetson) is that frame, not part of the device.
  const partGroup = listRow?.group ?? part?.group; // the list's own group label
  const parentJoint = joint && jointsOf(payload).find((j) => j.child === joint.parent);
  // `details` is always visible; `more` sits behind "Details".
  const details: Row[] = groupHead
    ? [
        ...(groupHead.kind ? [{ label: "Kind", value: KIND_WORDS[groupHead.kind] ?? groupHead.kind }] : []),
        { label: "Parts", value: String(groupRows.length) }, // the tree's own (folded) rows, so it matches the group badge
        ...(["fault", "warn", "ok"].includes(worstOf(groupParts.map((p) => p.state)) ?? "") ? [{ label: "State", value: toCard(stateValue(worstOf(groupParts.map((p) => p.state)))) }] : []),
        { label: "Findings", value: findingsOf },
      ]
    : part
    ? [
        ...(partGroup && partGroup.label !== listName && !(partGroup.kind === "device" && part.mount_frame === part.node) ? [{ label: "Part of", value: <Jumper label={`Select the ${partGroup.label} group`} onClick={() => setSelectedGroup({ id: partGroup.id, members: rows.filter((r) => r.group?.id === partGroup.id).flatMap((r) => r.sels ?? [r.id]) })}>{partGroup.label}</Jumper> }] : []),
        ...(["fault", "warn", "ok"].includes(part.state) ? [{ label: "State", value: toCard(stateValue(part.state)) }] : []),
        { label: "Findings", value: findingsOf },
      ]
    : isRoot
    ? [{ label: "Kind", value: "Root part" }, { label: "Attached", value: "Every other part hangs off it" }]
    : joint
      ? joint.type === "fixed"
        ? [{ label: "Attached", value: <>Fixed to {parentJoint ? <Jumper label={`Select ${joint.parent}`} onClick={() => setSelectedPartId(parentJoint.name)}>{joint.parent_name}</Jumper> : joint.parent_name}</> }]
        : [{ label: "Type", value: joint.type }, { label: "From", value: parentJoint ? <Jumper label={`Select ${joint.parent}`} onClick={() => setSelectedPartId(parentJoint.name)}>{joint.parent}</Jumper> : joint.parent }]
      : [];
  const lensRow: Row[] = partLenses ? [{ label: "Lens", value: <>{lensWords.map((l, i) => (
    <span key={l}>{i > 0 && ", "}{l === view.lens ? l.charAt(0).toUpperCase() + l.slice(1)
      : <Jumper label={`Show the ${l} lens`} onClick={() => setDesignView({ lens: l })}>{l.charAt(0).toUpperCase() + l.slice(1)}</Jumper>}</span>
  ))}</> }] : [];
  const more: Row[] = groupHead
    ? []
    : joint
      ? [
          { label: "Joint", value: joint.name },
          ...(joint.lower !== null || joint.upper !== null ? [{ label: "Lower", value: limit(joint.lower) }, { label: "Upper", value: limit(joint.upper) }] : []),
          ...(joint.effort !== null ? [{ label: "Effort", value: <Figure value={Number(joint.effort.toPrecision(4))} unit={joint.type === "prismatic" ? "N" : "N·m"} kind="declared" /> }] : []),
        ]
      : [];
  if (joint) {
    const words = placementWords(joint.rpy ?? []);
    more.push({ label: "Placement", value: words.rpy }, { label: "Quaternion", value: words.quaternion });
  }
  more.push(...lensRow);
  if (scenePayload) {
    const name = String(scenePayload.asset_path ?? scenePayload.source).split("/").pop();
    more.push({ label: "Model", value: <span className="scene-asset-identity" title={assetIdentity}>{name}</span> });
  }
  const mass = <MassReadout payload={payload} edit={contract && expectedRevision && onEdit ? { contract, expectedRevision, onEdit, onReopen, onPayload } : undefined} />;
  if (!part && !joint && !groupHead && !isRoot) {
    return <>{mass}{jointsOf(payload).some(isMovable) && <Panel title="Pose"><PoseControls joints={jointsOf(payload)} /></Panel>}{analysis}</>;
  }
  // `move` SETS a link's or device's position, so the typed move needs where it sits now: a device's mount_xyz, or the joint origin that places the link (a joint row edits its child link).
  const mover = joint ?? carrier;
  const refDevice = payload && "faces" in payload ? payload.faces.reference.devices.find((d) => d.id === selectedPartId) : undefined;
  const moveFrom = (refDevice?.mount_mm ?? mover?.origin_mm) as [number, number, number] | undefined;
  const moveTarget = joint ? joint.child : selectedPartId;
  // What Delete removes: a joint row is the part it carries (its child link), a device (placed, unplaced or wired) is a device, a known link is a link; anything else sends nothing.
  const isDevice = !!refDevice || !!unplaced || part?.group?.kind === "device" || !!(payload && "wiring_devices" in payload && payload.wiring_devices?.some((d) => d.id === selectedPartId));
  const deleteLabel = listName ?? joint?.child_name ?? selectedPartId ?? "";
  deleteTargetRef.current = joint ? { kind: "link", name: joint.child, label: deleteLabel }
    : selectedPartId && isDevice ? { kind: "device", name: selectedPartId, label: deleteLabel }
      : selectedPartId && !isRoot && linksOf(payload).includes(selectedPartId) ? { kind: "link", name: selectedPartId, label: deleteLabel } : null;
  const canSwap = !!(contract && expectedRevision && onSwapped);
  // Print this part: the link the selection stands for, and whether the payload draws a shape for it.
  const printLink = joint?.child ?? carrier?.child ?? (selectedPartId && linksOf(payload).includes(selectedPartId) ? selectedPartId : null);
  const printShape = !!printLink && (!!payload && "links" in payload && payload.links.some((l) => l.name === printLink && (l.solids?.length ?? 0) > 0) || !!scenePayload?.parts.some((p) => p.node === printLink && p.positions.data));
  if (swapJoint && canSwap) {
    return <SwapPanel contract={contract!} revision={expectedRevision!} joint={swapJoint} onClose={() => setSwapJoint(null)} onApplied={(lines) => { setSwapJoint(null); onSwapped!(lines); }} />;
  }
  return (
    <>
      <Panel title={groupHead?.label ?? listName ?? (joint && joint.child_name) ?? "Inspector"}>
        <div className="design-face">
          <>
          {details.length > 0 && <Inspector rows={details} />}
          {(joint ?? carrier) && <TipReadout joint={(joint ?? carrier)!} />}
          <PoseControls joints={jointsOf(payload)} selected={joint ?? carrier} />
          {canSwap && joint && (joint.type === "revolute" || joint.type === "continuous") && (
            <Button variant="secondary" onClick={() => setSwapJoint(joint.name)}>Swap servo</Button>
          )}
          {contract && printLink && <PrintPart contract={contract} link={printLink} hasShape={printShape} />}
          <Disclosure summary="Details"><Inspector rows={more} /></Disclosure>
          {!groupHead && !isRoot && contract && expectedRevision && onEdit && onReopen && onPayload && (
            <Disclosure summary="Edit placement" open={unplaced ? true : undefined}>
              {part?.group.kind === "device" && onPlace && (
                !scenePayload && linksOf(payload).length === 0 ? <p>This robot has no 3D model yet, so there is nothing to place this on.</p> : (
                <>
                  <Field label="Place on…" control="select" aria-label="Place on" value="" onChange={(e: { target: { value: string } }) => { const frame = e.target.value; if (frame) void onPlace(part.id ?? part.node, frame).then(setPlaceRefusal); }}>
                    <option value="">Pick a frame</option>
                    {linksOf(payload).map((l) => <option key={l} value={l}>{l}</option>)}
                  </Field>
                  {placeRefusal && <Notice kind="error">{placeRefusal}</Notice>}
                </>
                )
              )}
              <DesignControls
                contract={contract}
                expectedRevision={expectedRevision}
                selectedPartId={moveTarget}
                selectedPartName={listName ?? joint?.child_name}
                positionMm={moveFrom?.length === 3 ? moveFrom : null}
                rpyRad={mover?.rpy?.length === 3 ? (mover.rpy as [number, number, number]) : null}
                onEdit={onEdit}
                onReopen={onReopen}
                onPayload={onPayload}
              />
            </Disclosure>
          )}
          </>
        </div>
      </Panel>
      {mass}
      {analysis}
    </>
  );
}
