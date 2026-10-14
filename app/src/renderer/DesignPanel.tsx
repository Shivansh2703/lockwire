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
import { useEffect } from "react";
import * as THREE from "three";

import type { CheckPayload, PagePayload, RobotJoint, SceneLabel, ScenePart as ApiScenePart } from "../shared/api.gen";
import { useScene } from "./scene/Canvas";
import { DesignControls, type DesignEditReply, type DesignEditRequest } from "./DesignControls";
import { CalloutLayer, type CalloutAnchor } from "./scene/CalloutLayer";
import { Disclosure, EmptyState, IconButton, Inspector, Notice, Panel, PartTree, PillTabs, Figure, type Status } from "./kit";
import { highlightsFromCards } from "./scene/robotScene";
import { LENS_IDS, type LensedPart } from "./scene/lenses";
import { pro } from "./proRegistry";
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
const KIND_WORDS: Record<string, string> = { device: "Device", rail: "Power rail", bus: "Data bus", link: "Link", other: "Structure" };

/** Parts come from the mesh scene: one row per mesh node, listed under the engine's own `group`
 * (the app never works out which component a part belongs to). A robot with no mesh
 * (QArm, ANYmal) lists its links as a tree from the root, each row picking the joint that carries
 * the link, dotted from the Check cards' own findings; each top-level chain (a leg) is a group
 * when there is more than one. */
/** "base_link" -> "Base", "END-EFFECTOR" -> "End-effector". */
const plainLink = (name: string) => { const w = name.replace(/_link$/i, "").replace(/_/g, " ").toLowerCase(); return w.charAt(0).toUpperCase() + w.slice(1); };
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
    return [...folded.values()].map(({ n, ...row }) => (n > 1 ? { ...row, name: `${row.name} ×${n}` } : row));
  }
  const joints = jointsOf(payload);
  const level = new Map(highlightsFromCards(payload && "cards" in payload ? payload.cards : [], joints).map((h) => [h.id, h.level]));
  const out: TreeRow[] = [];
  const walk = (link: string, depth: number, seen: Set<string>, group?: { id: string; label: string }) => {
    for (const joint of joints.filter((j) => j.parent === link && !seen.has(j.name))) {
      const state: Status = level.get(joint.name) === "bad" ? "bad" : level.get(joint.name) === "warn" ? "warn" : "go";
      const g = group ?? { id: joint.name, label: joint.child };
      out.push({ id: joint.name, name: plainLink(joint.child), state, depth, group: g });
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
  const rows = rowsOf(payload);
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
            onChange={(id) => setDesignView({ lens: id })}
            tabs={LENS_IDS.map((id) => ({ id, label: id === "off" ? "All" : graph[id].label }))}
          />
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
  const { callouts } = useDesignView();
  const scene = sceneOf(payload);
  return (
    <>
      <SceneCapture />
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
}: {
  payload: Payload;
  contract?: string;
  expectedRevision?: string;
  onEdit?: (request: DesignEditRequest) => Promise<DesignEditReply>;
  onReopen?: (contract: string) => Promise<CheckPayload | PagePayload>;
  onPayload?: (payload: CheckPayload | PagePayload) => void;
}) {
  const selectedPartId = useSelectedPartId();
  const selectedGroup = useSelectedGroup();
  const view = useDesignView();
  const scenePayload = sceneOf(payload);
  const sceneKey = scenePayload ? `${contract ?? "preview"}:${scenePayload.hash}` : undefined;
  const rows = rowsOf(payload);
  const assetIdentity = scenePayload?.expected_asset_sha256
    ? `Selected asset ${scenePayload.asset_path ?? scenePayload.source} matches the declared model identity ${scenePayload.expected_asset_sha256}.`
    : `Selected scene ${scenePayload?.asset_path ?? scenePayload?.source}; declared model identity is unavailable.`;

  useEffect(() => {
    setSelectionSceneKey(sceneKey);
    return () => setSelectionSceneKey(undefined);
  }, [sceneKey]);
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

  const groupParts = selectedGroup ? scenePayload?.parts.filter((p) => p.group.id === selectedGroup.id) ?? [] : [];
  const groupRows = selectedGroup ? rows.filter((r) => r.group?.id === selectedGroup.id) : [];
  const groupHead = groupParts[0]?.group ?? groupRows[0]?.group;
  const part = scenePayload?.parts.find((candidate) => (candidate.id ?? candidate.node) === selectedPartId);
  const joint = part ? undefined : jointsOf(payload).find((candidate) => candidate.name === selectedPartId);
  const unit = joint ? UNIT[joint.type] : undefined;
  const limit = (value: number | null) => value === null ? "none" : <Figure value={Number(value.toPrecision(4))} unit={unit} kind="declared" />;
  const findings = part ? scenePayload!.labels.filter((label) => label.part === part.id).length : 0;
  const partsInGroup = part ? rows.filter((r) => r.group?.id === part.group.id).length : 0; // folded rows, like the badge
  const partLenses = part?.lenses.filter((l) => l !== "off").map((l) => l.charAt(0).toUpperCase() + l.slice(1)).join(", ");
  const worstOf = (states: string[]) => (["fault", "warn", "affected", "ok", "unchecked", "plain"] as const).find((st) => states.includes(st));
  type Row = { label: string; value: React.ReactNode };
  const stateWord = (st?: string) => (st === "fault" ? "Fails" : st === "warn" ? "Check" : undefined);
  const stateRow = (st?: string): Row[] => (stateWord(st) ? [{ label: "State", value: stateWord(st) }] : []);
  const findingsRow = (n: number): Row[] => (n > 0 ? [{ label: "Findings", value: <Figure value={n} kind="derived" /> }] : []);
  const whyRow = (id: string | null): Row[] => { const t = scenePayload!.labels.find((l) => l.part === id)?.title; return t ? [{ label: "Why", value: t }] : []; };
  const groupFindings = groupParts.length ? scenePayload!.labels.filter((l) => groupParts.some((p) => p.id === l.part)).length : 0;
  // `details` is always visible; `more` sits behind "Details".
  const details: Row[] = groupHead
    ? [
        ...(groupHead.kind ? [{ label: "Kind", value: KIND_WORDS[groupHead.kind] ?? groupHead.kind }] : []),
        { label: "Parts", value: String(groupRows.length) }, // the tree's own (folded) rows, so it matches the group badge
        { label: "State", value: PART_STATE_WORDS[worstOf(groupParts.map((p) => p.state)) ?? "unchecked"] },
        { label: "Findings", value: groupFindings > 0 ? <Figure value={groupFindings} kind="derived" /> : "No findings" },
      ]
    : part
    ? [
        { label: "Group", value: part.group.label !== part.name ? [part.group.label, KIND_WORDS[part.group.kind ?? ""]].filter(Boolean).join(" · ") : `${partsInGroup} part${partsInGroup === 1 ? "" : "s"}` },
        { label: "State", value: PART_STATE_WORDS[part.state] },
        ...whyRow(part.id),
        { label: "Findings", value: findings > 0 ? <Figure value={findings} kind="derived" /> : "No findings" },
        ...(partLenses ? [{ label: "Lens", value: partLenses }] : []),
      ]
    : joint
      ? [{ label: "Type", value: joint.type }, { label: "From", value: joint.parent }]
      : [];
  const more: Row[] = groupHead
    ? []
    : joint
      ? [
          { label: "Joint", value: joint.name },
          ...(joint.lower !== null || joint.upper !== null ? [{ label: "Lower", value: limit(joint.lower) }, { label: "Upper", value: limit(joint.upper) }] : []),
          ...(joint.effort !== null ? [{ label: "Effort", value: <Figure value={Number(joint.effort.toPrecision(4))} unit={joint.type === "prismatic" ? "N" : "N·m"} kind="declared" /> }] : []),
        ]
      : [];
  if (scenePayload) {
    const name = String(scenePayload.asset_path ?? scenePayload.source).split("/").pop();
    more.push({ label: "Model", value: <span className="scene-asset-identity" title={assetIdentity}>{name}</span> });
  }
  if (!part && !joint && !groupHead) {
    return (
      <>
        <Panel title="Inspector"><div className="design-face" role="status">Pick a part</div></Panel>
        {analysis}
      </>
    );
  }
  return (
    <>
      <Panel title={groupHead?.label ?? part?.name ?? (joint && plainLink(joint.child)) ?? "Inspector"}>
        <div className="design-face">
          <>
          {details.length > 0 && <Inspector rows={details} />}
          <Disclosure summary="Details"><Inspector rows={more} /></Disclosure>
          {!groupHead && contract && expectedRevision && onEdit && onReopen && onPayload && (
            <Disclosure summary="Edit placement">
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
