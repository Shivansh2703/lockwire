/**
 * RobotView: the reusable themed robot canvas. Mesh robots draw their parts; robots with no mesh
 * (QArm) draw a joint/link skeleton from the page payload. Built on `Canvas`/`buildScene`.
 * `highlight` tints parts/joints by level, `focus` pans the camera to ids, `onPick` reports the
 * picked part id (skeleton: the joint name). Selection falls back to the shared store.
 */
import { useEffect, useMemo, type ReactNode } from "react";

import type { RobotJoint, RobotLink } from "../../shared/api.gen";
import { Canvas, useScene } from "./Canvas";
import type { SceneHit, ScenePart, ViewInset } from "./buildScene";
import type { LensedPart, LensId } from "./lenses";
import { applyDim, applyHighlight, buildSkeleton, focusCamera, type Highlight } from "./robotScene";
import { useSelectedPartId } from "./selectionStore";

/** A fixed joint has no motor of its own: selecting it lights both links it joins. */
function withLinks(ids: string[], joints: RobotJoint[]): string[] {
  const out = new Set(ids);
  for (const j of joints) if (out.has(j.name) && j.type === "fixed") { out.add(j.parent); out.add(j.child); }
  return [...out];
}

function Effects({ highlight, selected, focus, dim, joints }: { highlight: Highlight[]; selected: string[]; focus?: string[]; dim: boolean; joints: RobotJoint[] }) {
  const ctx = useScene();
  const selKey = selected.join("\u0000");
  useEffect(() => { applyHighlight(ctx, highlight, withLinks(selected, joints)); applyDim(ctx, dim); }, [ctx, highlight, selKey, dim, joints]); // eslint-disable-line react-hooks/exhaustive-deps
  const focusKey = focus?.join("\u0000");
  useEffect(() => {
    if (focus?.length) focusCamera(ctx, focus);
  }, [ctx, focusKey]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

export function RobotView({
  sceneKey, parts = [], joints = [], links = [], highlight = [], selected, focus, onPick, onDropPart, lens = "off", dim = false, children, viewInset,
}: {
  sceneKey: string;
  parts?: (ScenePart & LensedPart)[];
  joints?: RobotJoint[];
  links?: RobotLink[];
  highlight?: Highlight[];
  selected?: string | string[] | null;
  focus?: string[];
  onPick?: (id: string | null) => void;
  onDropPart?: (part: string, hit: SceneHit | null) => void;
  /** Design lens: dims parts outside it. */
  lens?: LensId;
  /** Docs: the robot at ~40% opacity, grid behind it. */
  dim?: boolean;
  /** Mounted inside the canvas (callout layers, capture hooks). */
  children?: ReactNode;
  /** Px under floating UI (tabs, panels): the robot is framed in the rest. */
  viewInset?: ViewInset;
}) {
  const stored = useSelectedPartId();
  const skeleton = useMemo(
    () => (parts.length === 0 && joints.length > 0 ? buildSkeleton(joints, links) : undefined),
    [parts.length, joints, links],
  );
  return (
    <Canvas parts={parts} sceneKey={sceneKey} lens={lens} skeleton={skeleton} onPick={onPick} onDropPart={onDropPart} inset={viewInset}>
      <Effects highlight={highlight} selected={[(selected === undefined ? stored : selected) ?? []].flat()} focus={focus} dim={dim} joints={joints} />
      {children}
    </Canvas>
  );
}
