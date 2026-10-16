import type { RobotJoint, RobotLink } from "../../shared/api.gen";
import type { ContractWriteRequest } from "../../shared/ipc-contract";
import type { SceneHit } from "./buildScene";

/** Drag type a listed device carries; the canvas accepts only this. */
export const PART_MIME = "application/x-lockwire-part";

type Place = Extract<ContractWriteRequest, { command: "place" }>["args"];
export type Prov = Pick<Place, "contract" | "source" | "witness" | "expected_revision">;

/** The `place` edit that writes mount_frame + mount_xyz (metres, in that frame). */
export const placeRequest = (target: string, on: string, offset: [number, number, number], prov: Prov): ContractWriteRequest =>
  ({ command: "place", args: { ...prov, target, on, offset, unit: "m" } });

/**
 * The edit for a part dropped where the ray hit `hit`, or null when nothing was hit or the hit
 * part is no known frame. A hit on a device that declares a mount_frame uses that frame (`mounts`);
 * otherwise a joint, a joint's child or a link. The mount point is the world hit minus the frame's origin.
 * ponytail: translation only, so it assumes the frame's axes are parallel to the world's; the
 * payload carries no link rotations. Add rotation when it does.
 */
export function dropEdit(
  device: string, hit: SceneHit | null,
  scene: { joints: RobotJoint[]; links: RobotLink[]; mounts?: Record<string, string> }, prov: Prov,
): ContractWriteRequest | null {
  if (!hit) return null;
  const mount = scene.mounts?.[hit.id];
  const joint = scene.joints.find((j) => j.name === (mount ?? hit.id) || j.child === (mount ?? hit.id));
  const link = scene.links.find((l) => l.name === (mount ?? hit.id));
  const frame = joint?.child ?? link?.name;
  const origin = joint?.position ?? link?.position;
  if (!frame || !origin || frame === device) return null;
  const at = hit.point.map((c, i) => Math.round((c - (origin[i] ?? 0)) * 1e4) / 1e4);
  return placeRequest(device, frame, at as [number, number, number], prov);
}
