import { useState } from "react";

import { useDismiss } from "./MoreMenu";
import { useSelectedGroup, useSelectedPartId } from "./scene/selectionStore";
import { savePicture } from "./scene/savePicture";
import type { CameraView } from "./shortcuts";
import { setUnits, useUnits } from "./units";
import { cameraView, setCentreOfMass, useViewState, viewAction, viewChanged, type ViewAction } from "./scene/viewState";

/** VIEW-1: the one compact View control beside Fit view. Everything here changes the picture only, never the lockfile. */
export function ViewMenu({ design, name }: { design: boolean; name?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const v = useViewState();
  const units = useUnits();
  const picked = [useSelectedPartId(), useSelectedGroup()].some(Boolean);
  const row = (label: string, key: string, action: ViewAction, on: boolean, off = false) => (
    <button type="button" role="menuitemcheckbox" aria-checked={on} disabled={off} className="kit-tab view-row" onClick={() => viewAction(action)}>
      <span>{label}</span><kbd>{key}</kbd>
    </button>
  );
  const look = (label: string, key: string, v: CameraView) => (
    <button type="button" role="menuitem" className="kit-tab view-row" onClick={() => cameraView(v)}><span>{label}</span><kbd>{key}</kbd></button>
  );
  const note = [v.hidden.length && `${v.hidden.length} hidden`, v.only.length && `${v.only.length} shown alone`].filter(Boolean).join(", ");
  return (
    <div className="shell-menu view-menu" ref={ref}>
      <button type="button" className="kit-btn kit-btn-quiet" aria-haspopup="menu" aria-expanded={open} data-active={viewChanged(v) ? "" : undefined} onClick={() => setOpen(!open)}>View ▾</button>
      {open && (
        <div className="shell-menu-list view-menu-list" role="menu" aria-label="View">
          {row("Hide selected part", "H", "hide", !!picked && v.hidden.length > 0, !picked)}
          {row("Show only selected part", "I", "only", v.only.length > 0, !picked)}
          {row("See through", "T", "seeThrough", v.seeThrough)}
          {row("Wireframe", "M", "wireframe", v.wireframe)}
          {row("Frames and axes", "L", "frames", v.frames)}
          {look("Front", "1", "front")}
          {look("Back", "Shift+1", "back")}
          {look("Left side", "3", "side")}
          {look("Right side", "Shift+3", "left")}
          {look("Top", "7", "top")}
          {look("Bottom", "Shift+7", "bottom")}
          {look("Corner view", "0", "iso")}
          {row("Flat view (no perspective)", "5", "ortho", v.ortho)}
          {design && <button type="button" role="menuitemcheckbox" aria-checked={v.com} className="kit-tab view-row" onClick={() => setCentreOfMass(!v.com)}><span>Centre of mass</span></button>}
          <button type="button" role="menuitem" className="kit-tab view-row view-reset" disabled={!viewChanged(v)} onClick={() => viewAction("showAll")}><span>Show everything</span><kbd>Shift+H</kbd></button>
          <button type="button" role="menuitem" className="kit-tab view-row" onClick={() => { setOpen(false); void savePicture(name ?? "robot").catch(() => undefined); }}><span>Save picture</span></button>
          <div className="view-row view-units" role="group" aria-label="Units">
            <span>Units</span>
            <span>
              {(["mm", "m"] as const).map((u) => <button key={u} type="button" role="menuitemradio" aria-checked={units.length === u} className="kit-tab" onClick={() => setUnits({ length: u })}>{u}</button>)}
              {(["deg", "rad"] as const).map((u) => <button key={u} type="button" role="menuitemradio" aria-checked={units.angle === u} className="kit-tab" onClick={() => setUnits({ angle: u })}>{u}</button>)}
            </span>
          </div>
          {note && <p className="view-note">{note}</p>}
        </div>
      )}
    </div>
  );
}
