import { useEffect, useMemo, useRef, useState } from "react";

import type { PagePayload } from "../shared/api.gen";
import { onMac } from "./shortcuts";
import "./FindPart.css";

// FIND-1: Cmd/Ctrl+F finds a link, joint, part or wire by name. Closed until asked; picking a match
// goes through the App's own selection (the same handler a click on that part runs), which frames it.
export type FindItem = { id: string; label: string; kind: "link" | "joint" | "device" | "wire" };

/** Everything named in the robot's page, in the order a person thinks of it. */
export function findItems(page: PagePayload | null | undefined): FindItem[] {
  if (!page) return [];
  return [
    ...page.links.map((l): FindItem => ({ id: l.name, label: l.name, kind: "link" })),
    ...page.joints.map((j): FindItem => ({ id: j.name, label: j.name, kind: "joint" })),
    ...(page.wiring_devices ?? []).map((d): FindItem => ({ id: d.id, label: d.name || d.id, kind: "device" })),
    ...(page.wiring_wires ?? []).map((w): FindItem => ({ id: w.id, label: w.name || w.id, kind: "wire" })),
  ];
}

const MAX = 8;
export const isFindKey = (e: Pick<KeyboardEvent, "key" | "metaKey" | "ctrlKey" | "altKey" | "shiftKey">, mac: boolean) =>
  e.key.toLowerCase() === "f" && (mac ? e.metaKey : e.ctrlKey) && !e.altKey && !e.shiftKey;

export function FindPart({ page, onPick }: { page: PagePayload | null | undefined; onPick: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [at, setAt] = useState(0);
  const back = useRef<Element | null>(null);
  const items = useMemo(() => findItems(page), [page]);
  const q = text.trim().toLowerCase();
  const hits = q ? items.filter((i) => i.label.toLowerCase().includes(q) || i.id.toLowerCase().includes(q)).slice(0, MAX) : [];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!isFindKey(e, onMac())) return;
      e.preventDefault();
      if (!open) back.current = document.activeElement;
      setText(""); setAt(0); setOpen(true);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const close = () => { setOpen(false); (back.current as HTMLElement | null)?.focus?.(); };
  const pick = (id: string) => { close(); onPick(id); };
  if (!open) return null;
  return (
    <div className="find-part" role="search" aria-label="Find a part">
      <input
        autoFocus
        aria-label="Find a part"
        placeholder="Find a link, joint, part or wire"
        value={text}
        onChange={(e) => { setText(e.target.value); setAt(0); }}
        onKeyDown={(e) => {
          if (e.key === "Escape") { e.preventDefault(); close(); }
          else if (e.key === "Enter" && hits[at]) { e.preventDefault(); pick(hits[at]!.id); }
          else if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); setAt((a) => Math.max(0, Math.min(hits.length - 1, a + (e.key === "ArrowDown" ? 1 : -1)))); }
        }}
      />
      {q && !hits.length && <p className="find-part-none">Nothing named {text.trim()}</p>}
      {hits.length > 0 && (
        <ul role="listbox">
          {hits.map((h, i) => (
            <li key={`${h.kind}:${h.id}`} role="option" aria-selected={i === at} className={i === at ? "on" : undefined} onMouseDown={(e) => { e.preventDefault(); pick(h.id); }}>
              <span>{h.label}</span><small>{h.kind}</small>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
