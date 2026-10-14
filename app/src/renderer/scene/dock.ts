// The docking pass (plan v8 line 547): keep every callout inside the panel
// and clear of every other callout. Pure geometry — no Canvas, no camera, no
// React — so it needs nothing that is still queued upstream.

export interface CalloutBox {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface Panel {
  width: number;
  height: number;
}

function overlaps(a: CalloutBox, b: CalloutBox): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}

/** Clamps each box inside the panel, then nudges any box that still overlaps
 * an already-docked one straight down until clear.
 * ponytail: O(n^2) greedy vertical nudge, fine at the label counts a person
 * can actually read on one part count; replace with a real layout solver only
 * if a contract ever produces hundreds of simultaneous callouts. */
export function dockCallouts(boxes: readonly CalloutBox[], panel: Panel): CalloutBox[] {
  const docked: CalloutBox[] = [];
  for (const box of boxes) {
    let next: CalloutBox = {
      ...box,
      x: Math.min(Math.max(box.x, 0), Math.max(panel.width - box.width, 0)),
      y: Math.min(Math.max(box.y, 0), Math.max(panel.height - box.height, 0)),
    };
    let moved = true;
    while (moved) {
      moved = false;
      for (const placed of docked) {
        if (overlaps(next, placed)) {
          next = { ...next, y: placed.y + placed.height };
          moved = true;
        }
      }
    }
    docked.push(next);
  }
  return docked;
}
