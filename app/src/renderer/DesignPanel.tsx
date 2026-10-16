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
import { useEffect, useState } from "react";
import * as THREE from "three";

import type { CheckPayload, PagePayload, RobotJoint, SceneLabel, ScenePart as ApiScenePart } from "../shared/api.gen";
import { useScene } from "./scene/Canvas";
import { DesignControls, type DesignEditReply, type DesignEditRequest } from "./DesignControls";
import { CalloutLayer, type CalloutAnchor } from "./scene/CalloutLayer";
import { Icon } from "./kit/icons";
import { limbOf } from "./kit/limb";
import { Disclosure, EmptyState, Field, IconButton, Inspector, Jumper, Notice, Panel, PartTree, PillTabs, Figure, type Status } from "./kit";
import type { OnJump } from "./jump";
import { highlightsFromCards } from "./scene/robotScene";
import { LENS_IDS, type LensedPart } from "./scene/lenses";
import { pro } from "./proRegistry";
import { PRODUCT_NAME } from "../shared/branding";
import { ConnectionLines, ConnectionsPanel } from "./Connections";
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
      camera={ctx.camera}
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
const DOT_WORDS: Partial<Record<Status, string>> = { bad: "fault", warn: "warn", go: "ok", muted: "unchecked" };
const PART_STATE_WORDS: Record<string, string> = { fault: "Fails", warn: "Check", affected: "Affected", ok: "OK", unchecked: "Not checked", plain: "Not checked" };
const stateValue = (state: string | undefined) => { const st = STATE[state as ApiScenePart["state"]] ?? "muted"; const w = PART_STATE_WORDS[state ?? "unchecked"]; return <span className="design-state"><i className={`kit-dot kit-s-${st}`} aria-hidden="true" />{w}</span>; };
const KIND_WORDS: Record<string, string> = { device: "Device", rail: "Power rail", bus: "Data bus", link: "Link", other: "Structure" };

/** Parts come from the mesh scene: one row per mesh node, listed under the engine's own `group`
 * (the app never works out which component a part belongs to). A robot with no mesh
 * (QArm, ANYmal) lists its links as a tree from the root, each row picking the joint that carries
 * the link, dotted from the Check cards' own findings; each top-level chain (a leg) is a group
 * when there is more than one. */
function rowsOf(payload: Payload): TreeRow[] {
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

/** LEFT: the part tree. Two-way with the 3D pick through the selection store; a group header
 * selects every part under it. */
export function DesignParts({ payload }: { payload: Payload }) {
  const selected = useSelectedPartId();
  const group = useSelectedGroup();
  const { lens } = useDesignView();
  // Rails and wires belong to the Power / Data lens (the connection list), not the part tree.
  const rows = rowsOf(payload).filter((r) => !payload?.graph || (r.group?.kind !== "rail" && r.group?.kind !== "bus"));
  if (payload?.graph && CONN_LENSES.includes(lens)) {
    const drawn = new Set(sceneOf(payload)?.parts.map((p) => p.id ?? p.node));
    return <ConnectionsPanel payload={payload} lens={lens as ConnLens} drawn={drawn} />;
  }
  if (rows.length === 0) return null;
  const words = WORST.filter((st) => rows.some((r) => r.state === st));
  return (
    <Panel title="Parts">
      <PartTree parts={rows} selected={selected ?? undefined} onSelect={setSelectedPartId} selectedGroup={group?.id} onSelectGroup={(id, members) => setSelectedGroup({ id, members })} />
      <p className="design-legend" aria-label="Dot legend">
        {words.map((st) => <span key={st}><i className={`kit-dot kit-s-${st}`} aria-hidden="true" />{DOT_WORDS[st]}</span>)}
      </p>
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
          {!pro && <span className="shell-pro-tab" aria-disabled="true" title={`Health is in ${PRODUCT_NAME} Pro`}>{graph.health.label}<span className="shell-pro-badge">Pro</span></span>}
        </div>
      )}
    </>
  );
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
  return (
    <>
      <SceneCapture />
      {CONN_LENSES.includes(lens) && <ConnectionLines payload={payload} lens={lens as ConnLens} />}
      {callouts && scene && <MountedCallouts labels={scene.labels} onOverflowChange={(overflow) => setDesignView({ overflow })} />}
    </>
  );
}

/** RIGHT: the Inspector for the selected part, "Edit placement", and the analysis panel. */
export function DesignPanel({
  payload,
  contract,
  expectedRevision,
  onEdit,
  onReopen,
  onPayload,
  onJump,
  onPlace,
}: {
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
}) {
  const [placeRefusal, setPlaceRefusal] = useState<string | null>(null);
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
        <Panel title="Design"><div className="design-face-empty"><EmptyState title="no Design face yet" /></div></Panel>
        {analysis}
      </>
    );
  }

  const groupRows = selectedGroup ? rows.filter((r) => r.group?.id === selectedGroup.id) : [];
  const groupParts = selectedGroup ? scenePayload?.parts.filter((p) => groupRows.some((r) => r.sels?.includes(p.id ?? p.node))) ?? [] : [];
  const groupHead = groupRows[0]?.group ?? groupParts[0]?.group; // the list's own group label (limb groups included)
  const part = scenePayload?.parts.find((candidate) => (candidate.id ?? candidate.node) === selectedPartId);
  // The inspector is titled with the name its list row shows (limb grouping may shorten a part's own name).
  const listRow = rows.find((r) => r.sels?.includes(selectedPartId ?? ""));
  const listBase = listRow?.name.replace(/ ×\d+$/, "");
  const listName = listRow && listBase ? (listRow.group?.id.startsWith("limb:") ? `${listRow.group.label} · ${listBase}` : listBase) : part?.name;
  const joint = part ? undefined : jointsOf(payload).find((candidate) => candidate.name === selectedPartId);
  const unit = joint ? UNIT[joint.type] : undefined;
  const limit = (value: number | null) => value === null ? "none" : <Figure value={Number(value.toPrecision(4))} unit={unit} kind="declared" />;
  const partLenses = part?.lenses.filter((l) => l !== "off").map((l) => l.charAt(0).toUpperCase() + l.slice(1)).join(", ");
  const worstOf = (states: string[]) => (["fault", "warn", "affected", "ok", "unchecked", "plain"] as const).find((st) => states.includes(st));
  type Row = { label: string; value: React.ReactNode };
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
  more.push(...lensRow);
  if (scenePayload) {
    const name = String(scenePayload.asset_path ?? scenePayload.source).split("/").pop();
    more.push({ label: "Model", value: <span className="scene-asset-identity" title={assetIdentity}>{name}</span> });
  }
  if (!part && !joint && !groupHead) {
    return analysis;
  }
  return (
    <>
      <Panel title={groupHead?.label ?? listName ?? (joint && joint.child_name) ?? "Inspector"}>
        <div className="design-face">
          <>
          {details.length > 0 && <Inspector rows={details} />}
          <Disclosure summary="Details"><Inspector rows={more} /></Disclosure>
          {!groupHead && contract && expectedRevision && onEdit && onReopen && onPayload && (
            <Disclosure summary="Edit placement">
              {part?.group.kind === "device" && onPlace && (
                <>
                  <Field label="Place on…" control="select" aria-label="Place on" value="" onChange={(e: { target: { value: string } }) => { const frame = e.target.value; if (frame) void onPlace(part.id ?? part.node, frame).then(setPlaceRefusal); }}>
                    <option value="">Pick a frame</option>
                    {linksOf(payload).map((l) => <option key={l} value={l}>{l}</option>)}
                  </Field>
                  {placeRefusal && <Notice kind="error">{placeRefusal}</Notice>}
                </>
              )}
              <DesignControls
                contract={contract}
                expectedRevision={expectedRevision}
                selectedPartId={selectedPartId}
                onEdit={onEdit}
                onReopen={onReopen}
                onPayload={onPayload}
              />
            </Disclosure>
          )}
          </>
        </div>
      </Panel>
      {analysis}
    </>
  );
}
