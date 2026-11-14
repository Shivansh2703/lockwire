import { useEffect, useRef, useState } from "react";

import type { Mode } from "./Frame";
import { PRODUCT_NAME as P } from "../shared/branding";

/** Close a popover on Esc or a press outside `ref`; focus goes back to what had it when it opened,
 * unless the user has already moved focus to another control outside it. */
export function useDismiss(open: boolean, close: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  const prev = useRef<Element | null>(null);
  const wasOpen = useRef(false);
  if (open && !wasOpen.current) prev.current = document.activeElement; // read in render: an autoFocus inside has not run yet
  wasOpen.current = open;
  useEffect(() => {
    if (!open) return;
    const node = ref.current;
    return () => {
      const now = document.activeElement;
      if (!now || now === document.body || node?.contains(now)) (prev.current as HTMLElement | null)?.focus?.();
    };
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const away = (e: Event) => { if (!ref.current?.contains(e.target as Node)) close(); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") close(); };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("pointerdown", away); document.removeEventListener("keydown", esc); };
  }, [open, close]);
  return ref;
}

/** The "More" button at the right end of the tab bar (popover anchors to the bar's left edge): a small menu of the screens that are not tabs. */
export function MoreMenu({ items, mode, onPick }: { items: { id: Mode; label: string }[]; mode: Mode; onPick: (m: Mode) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const active = items.find((i) => i.id === mode);
  return (
    <div className="shell-menu" ref={ref}>
      <button type="button" className="kit-tab shell-more" aria-haspopup="menu" aria-expanded={open} data-active={active ? "" : undefined} onClick={() => setOpen(!open)}>
        {active ? active.label : "More"} ▾
      </button>
      {open && (
        <div className="shell-menu-list" role="menu" aria-label="More">
          {items.map((i) => (
            <button key={i.id} type="button" role="menuitem" className="kit-tab" onClick={() => { setOpen(false); onPick(i.id); }}>{i.label}</button>
          ))}
        </div>
      )}
    </div>
  );
}

const INFO: Record<string, string[]> = {
  Design: ["See your robot and what is on it. Drag a part from the list onto the robot to place it.", "Next: Check tells you what will not work."],
  Check: [`${P} compares your robot with physics and the makers' datasheets. Red means it will not work as built.`, "Click a finding to see the number and where it came from."],
  Plan: [`Describe what the robot has to do. ${P} checks the robot can do it.`, "Next: fix what is red, then run Check again."],
  Declare: [`Give ${P} a number it should check against, like the weight on the datasheet.`, "Say where the number came from so others can trust it."],
  Test: [`${P} checks the robot's recorded numbers against the model while the robot is off.`, "Next: fix what is red, then test again."],
  Run: [`${P} compares what your robot reports, live or from a recording, with the model.`, "Green means it matches. Red means something has drifted."],
  Reference: ["Look up a part: what it is, what it needs, and where its numbers came from."],
};

/** The round "i" at the right end of the tab bar (popover anchors to the bar's left edge): one or two lines on what this screen is for. No text, no button. */
export function ModeInfo({ mode }: { mode: Mode }) {
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const lines = INFO[mode];
  if (!lines) return null;
  return (
    <div className="shell-info-wrap" ref={ref}>
      <button type="button" className="shell-info" aria-label="What is this screen for?" aria-expanded={open} aria-haspopup="true" onClick={() => setOpen(!open)}>i</button>
      {open && <div className="shell-info-pop" role="note">{lines.map((l) => <p key={l}>{l}</p>)}</div>}
    </div>
  );
}
