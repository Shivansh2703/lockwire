import type { MeshGap } from "../shared/api.gen";
import { Button } from "./kit";

// The mesh-folder fixer, one row under the verdict: `start` could not find the robot's meshes. One
// pick of the robot's package folder moves the lockfile there and re-checks; the count is `start`'s.
export interface EmptyGeometryPanelProps {
  gap: MeshGap;
  onChooseFolder: () => void;
  onSkip: () => void;
}

export function EmptyGeometryPanel({ gap, onChooseFolder, onSkip }: EmptyGeometryPanelProps) {
  return (
    <div className="empty-geometry-panel" role="status">
      <span><strong>Meshes not found ({gap.missing})</strong> Choose the robot's package folder to show its shapes.</span>
      <Button variant="primary" onClick={onChooseFolder}>Choose folder…</Button>
      <Button variant="quiet" onClick={onSkip}>Skip</Button>
    </div>
  );
}
