import type { RobotJoint } from "../shared/api.gen";
import { Button, Disclosure } from "./kit";
import { angleUnit, lenNum, lenUnit, useUnits } from "./units";
import { isMovable, isPosed, poseRange, setJointPose, useViewState, zeroPose } from "./scene/viewState";

/** POSE-1: one slider for one joint. Degrees for a turning joint, mm for a sliding one; the range is the joint's own limits. */
export function JointSlider({ joint }: { joint: RobotJoint }) {
  const { pose } = useViewState();
  const u = useUnits();
  const turns = joint.type !== "prismatic";
  // radians or metres to the shown unit; the slider step and the decimals follow the unit
  const [k, step, dp] = turns ? (u.angle === "rad" ? [1, 0.01, 2] : [180 / Math.PI, 0.5, 1]) : (u.length === "m" ? [1, 0.0005, 4] : [1000, 0.5, 1]);
  const [lo, hi] = poseRange(joint);
  const at = (v: number) => Number(v.toFixed(dp));
  const shown = at((pose[joint.name] ?? 0) * k);
  const unlimited = joint.lower == null || joint.upper == null;
  return (
    <label className="pose-slider">
      <span>{joint.child_name}{unlimited && <small className="pose-hint"> No limits declared; showing {turns ? "a half turn" : `${lenNum(100)} ${lenUnit()}`} each way</small>}</span>
      <input type="range" aria-label={`Pose ${joint.child_name}`} min={at(Math.floor((lo * k) / step) * step)} max={at(Math.ceil((hi * k) / step) * step)} step={step} value={shown} onChange={(e) => setJointPose(joint, Number(e.target.value) / k)} />
      <span className="pose-value">{shown} {turns ? angleUnit() : lenUnit()}</span>
    </label>
  );
}

/** The selected joint's slider, the one line that says the pose is not saved, and a way back; plus a small fold with every movable joint. Design only. */
export function PoseControls({ joints, selected }: { joints: RobotJoint[]; selected?: RobotJoint }) {
  const view = useViewState();
  const movable = joints.filter(isMovable);
  if (!movable.length) return null;
  return (
    <div className="pose-controls">
      {selected && isMovable(selected) && <JointSlider joint={selected} />}
      {isPosed(view) && (
        <div className="pose-note"><div><p>Posed for viewing only. Not saved.</p><p>Mass, torque and tip-over figures are for the zero pose.</p></div><Button variant="quiet" onClick={zeroPose}>Back to zero</Button></div>
      )}
      <Disclosure summary="Pose">{movable.map((j) => <JointSlider key={j.name} joint={j} />)}</Disclosure>
    </div>
  );
}
