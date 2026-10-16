import { useState } from "react";
import type { Starter } from "../shared/api.gen";
import { PRODUCT_NAME } from "../shared/branding";
import { Button, Icon, Panel } from "./kit";
import { ago, type Recent } from "./recents";
import { EmptyGeometryPanel, type EmptyGeometryPanelProps } from "./EmptyGeometryPanel";

// The first-launch screen: one "Open robot" button -- a model or a lockfile, one dialog --
// and a recent list (empty at first, honest about it). The dialog and the `start` command themselves live outside this component (the
// main process and the sidecar) -- this face only renders what it is given and reports
// what the user did, never a second file-open or a second copy of `start`'s output.

export interface OpenFaceProps {
  /** Genuinely empty on first launch -- no example rows (plan v8 line 430). */
  recent?: Recent[];
  onOpenRobot?: () => void;
  /** The bundled starters, as the sidecar lists them (empty until it answers). */
  starters?: Starter[];
  /** A starter's name, or null for a blank robot. */
  onNewRobot?: (starter: string | null) => void;
  onOpenRecent?: (path: string) => void;
  /** Set once the loaded contract has no geometry (plan v8 line 458). */
  emptyGeometry?: EmptyGeometryPanelProps;
}

// Short model name per bundled starter (full provenance stays in the tooltip); line glyphs, 20x20 grid.
const MODEL: Record<string, string> = { arm: "SO-101", drone: "Holybro X500", humanoid: "ToddlerBot", quadruped: "Solo-12", rover: "TurtleBot3" };
const GLYPH: Record<string, string> = {
  arm: "M4 17h6M7 17v-4l5-5M12 8l4 2M16 10a1.5 1.5 0 1 0 0 .1",
  drone: "M7 7l6 6M13 7l-6 6M4 4h3M13 4h3M4 16h3M13 16h3",
  humanoid: "M10 3a1.5 1.5 0 1 0 0 .1M6 8h8M10 8v5M10 13l-3 4M10 13l3 4",
  quadruped: "M4 8h12v3H4zM5 11v5M8 11v5M12 11v5M15 11v5",
  rover: "M3 11h14v3H3zM6 11V8h6l2 3M6 16a1 1 0 1 0 0 .1M14 16a1 1 0 1 0 0 .1",
  blank: "M10 5v10M5 10h10",
};
function Glyph({ name }: { name: string }) {
  return (
    <svg className="kit-icon open-face-glyph" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <path d={GLYPH[name] ?? GLYPH.blank} />
    </svg>
  );
}

export function OpenFace({
  recent = [],
  onOpenRobot,
  starters = [],
  onNewRobot,
  onOpenRecent,
  emptyGeometry,
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
          <Icon name="upload" />
          <p className="open-face-hint">Drop a model file</p>
          <div className="open-face-actions">
            <Button variant="primary" onClick={onOpenRobot}>
              Open robot
            </Button>
          </div>
        </div>

        <div className="open-face-new">
          <p className="open-face-label">New robot</p>
          <ul>
            {starters.map((s) => (
              <li key={s.name}>
                <Button title={`Based on ${s.based_on}`} onClick={() => onNewRobot?.(s.name)}>
                  <Glyph name={s.name} />
                  <span>
                    {s.title}
                    <small className="open-face-folder">{MODEL[s.name] ?? s.based_on}</small>
                  </span>
                </Button>
              </li>
            ))}
            <li data-blank>
              <Button variant="quiet" onClick={() => onNewRobot?.(null)}>
                <Glyph name="blank" />
                <span>
                  Blank
                  <small className="open-face-folder">Start empty</small>
                </span>
              </Button>
            </li>
          </ul>
        </div>

        <div className="open-face-recents">
          {recent.length > 0 && <p className="open-face-label">Recent</p>}
          {recent.length > 0 ? (
            <ul>
              {recent.map(({ path, name, at }) => (
                <li key={path}>
                  <Button variant="quiet" title={path} onClick={() => onOpenRecent?.(path)}>
                    {name}
                    <small className="open-face-folder">{[path.split(/[\\/]/).slice(-2, -1)[0], Number.isFinite(at) && ago(at as number)].filter(Boolean).join(" · ")}</small>
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
