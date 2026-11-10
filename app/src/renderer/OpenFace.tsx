import { useState } from "react";
import type { Starter } from "../shared/api.gen";
import { PRODUCT_NAME } from "../shared/branding";
import { Button, Panel } from "./kit";
import { ago, type Recent } from "./recents";

export const VIEW_HINT = "Drag to turn · two-finger scroll to move · pinch to zoom";

// The first-launch screen (mockup 1): one "Open robot" button -- a model or a lockfile, one
// dialog -- the starters demoted to small example chips, and a recent list (empty at first). The dialog and the `start` command themselves live outside this component (the
// main process and the sidecar) -- this face only renders what it is given and reports
// what the user did, never a second file-open or a second copy of `start`'s output.

export interface OpenFaceProps {
  /** The engine answers; until then every way in is disabled, so nothing fails with a raw error. */
  connected?: boolean;
  /** Genuinely empty on first launch -- no example rows (plan v8 line 430). */
  recent?: Recent[];
  onOpenRobot?: () => void;
  /** The bundled starters, as the sidecar lists them (empty until it answers). */
  starters?: Starter[];
  /** A starter's name, or null for a blank robot. */
  onNewRobot?: (starter: string | null) => void;
  onOpenRecent?: (path: string) => void;
}

// Short model name per bundled starter (full provenance stays in the tooltip).
const MODEL: Record<string, string> = { arm: "SO-101", drone: "Holybro X500", humanoid: "ToddlerBot", quadruped: "Solo-12", rover: "TurtleBot3" };
export function OpenFace({
  connected = true,
  recent = [],
  onOpenRobot,
  starters = [],
  onNewRobot,
  onOpenRecent,
}: OpenFaceProps) {
  const [drag, setDrag] = useState(false);
  return (
    <div className="open-face">
      <Panel>
        <div className="open-face-hero">
          <svg className="open-face-mark" viewBox="100 100 824 824" role="img" aria-label={PRODUCT_NAME}>
            <rect x="100" y="100" width="824" height="824" rx="185" fill="#1560cc" />
            <svg x="232" y="232" width="560" height="560" viewBox="0 0 64 64">
              <mask id="open-face-4i"><rect width="64" height="64" fill="#fff" /><circle cx="32" cy="36.5" r="4" fill="#000" /><path d="M30 37h4l1 8.5h-6z" fill="#000" /></mask>
              <g transform="translate(0 2)">
                <g mask="url(#open-face-4i)"><path d="M13 13V42a9.5 9.5 0 0 0 19 0M32 42a9.5 9.5 0 0 0 19 0V22" fill="none" stroke="#ffffff" strokeWidth="9" /><rect x="23" y="29" width="18" height="20" rx="3.5" fill="#ffffff" /></g>
                <path d="M13 13V5.5" stroke="#f2a35e" strokeWidth="5.5" strokeLinecap="round" />
              </g>
            </svg>
          </svg>
          <h1 className="open-face-wordmark">{PRODUCT_NAME}</h1>
        </div>
        <div
          className="open-face-drop"
          data-drag={drag}
          onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
          onDragLeave={() => setDrag(false)}
          onDrop={() => setDrag(false)}
        >
          {/* The drop itself bubbles to App's window-level handler (pathForFile -> openDroppedPath). */}
          <p className="open-face-hint">Drop a model file, or</p>
          <div className="open-face-actions">
            <Button variant="primary" disabled={!connected} onClick={onOpenRobot}>
              Open robot
            </Button>
          </div>
          <small className="open-face-hint">{connected ? "URDF, SDF, xacro or a lockfile" : "Starting up, one moment…"}</small>
        </div>

        <div className="open-face-new">
          <p className="open-face-label">OR TRY AN EXAMPLE</p>
          <ul>
            {starters.map((s) => (
              <li key={s.name}>
                <Button variant="quiet" disabled={!connected} title={`Based on ${s.based_on}`} onClick={() => onNewRobot?.(s.name)}>
                  {`${s.title} · ${MODEL[s.name] ?? s.based_on}`}
                </Button>
              </li>
            ))}
            <li>
              <Button variant="quiet" disabled={!connected} onClick={() => onNewRobot?.(null)}>Blank</Button>
            </li>
          </ul>
        </div>

        <div className="open-face-recents">
          <p className="open-face-label">RECENT</p>
          {recent.length > 0 ? (
            <ul>
              {recent.map(({ path, name, at }) => (
                <li key={path}>
                  <Button variant="quiet" disabled={!connected} title={path} onClick={() => onOpenRecent?.(path)}>
                    {name}
                    {Number.isFinite(at) && <small className="open-face-folder">{ago(at as number)}</small>}
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="open-face-recents-empty">No recent robots</p>
          )}
        </div>
      </Panel>
    </div>
  );
}
