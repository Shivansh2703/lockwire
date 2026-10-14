import { useState } from "react";
import { PRODUCT_NAME } from "../shared/branding";
import { Button, Icon, Panel } from "./kit";
import type { Recent } from "./recents";
import { EmptyGeometryPanel, type EmptyGeometryPanelProps } from "./EmptyGeometryPanel";

// The first-launch screen: one "Open robot" button -- a model or a lockfile, one dialog --
// and a recent list (empty at first, honest about it). The dialog and the `start` command themselves live outside this component (the
// main process and the sidecar) -- this face only renders what it is given and reports
// what the user did, never a second file-open or a second copy of `start`'s output.

export interface OpenFaceProps {
  /** Genuinely empty on first launch -- no example rows (plan v8 line 430). */
  recent?: Recent[];
  onOpenRobot?: () => void;
  onOpenRecent?: (path: string) => void;
  /** Set once the loaded contract has no geometry (plan v8 line 458). */
  emptyGeometry?: EmptyGeometryPanelProps;
}

export function OpenFace({
  recent = [],
  onOpenRobot,
  onOpenRecent,
  emptyGeometry,
}: OpenFaceProps) {
  const [drag, setDrag] = useState(false);
  return (
    <div className="open-face">
      <Panel>
        <div className="open-face-hero">
          <span className="open-face-mark"><Icon name="cube" /></span>
          <h1 className="open-face-wordmark">{PRODUCT_NAME}</h1>
        </div>
        <div
          className="open-face-drop"
          data-drag={drag}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={() => setDrag(false)}
        >
          <Icon name="upload" />
          <p className="open-face-hint">Drop a model file</p>
          <div className="open-face-actions">
            <Button variant="primary" onClick={onOpenRobot}>
              Open robot
            </Button>
          </div>
        </div>

        <div className="open-face-recents">
          {recent.length > 0 ? (
            <ul>
              {recent.map(({ path, name }) => (
                <li key={path}>
                  <Button variant="quiet" title={path} onClick={() => onOpenRecent?.(path)}>
                    {name}
                    {recent.filter((r) => r.name === name).length > 1 && (
                      <small className="open-face-folder">{path.split(/[\\/]/).slice(-2, -1)[0]}</small>
                    )}
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="open-face-recents-empty">No recent robots</p>
          )}
        </div>
      </Panel>

      {emptyGeometry && <EmptyGeometryPanel {...emptyGeometry} />}
    </div>
  );
}
