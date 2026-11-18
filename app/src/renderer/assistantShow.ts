// A show tool of the Assistant (main/assistantShow.ts) lands here. It moves the same stores and runs the
// same handlers a click does, so the 3D view and the graph change at once. View only: nothing is written.
import type { AssistantShow } from "../shared/ipc-contract";
import { setDesignView } from "./scene/designView";
import { setSelectedPartId } from "./scene/selectionStore";
import { setOnlyParts } from "./scene/viewState";

/** What only App holds; everything else goes straight to a store. */
export interface ShowHands {
  /** The open robot: a message for any other robot is dropped. */
  contract: string | undefined;
  /** The part ids the 3D view draws. */
  drawn: ReadonlySet<string>;
  /** What a click on a part runs after the selection is set. */
  pick: (part: string) => void;
  /** What a click on a finding runs. */
  finding: (index: number) => void;
  design: () => void;
  graph: (on: boolean) => void;
  /** The "Only what this check uses" switch. */
  isolate: (on: boolean) => void;
  /** Every part id the robot has (drawn or not). */
  known: ReadonlySet<string>;
  /** How many findings the page has. */
  cards: number;
}

const LENSES = ["power", "data", "mechanical"];
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every((x) => typeof x === "string");

/** A message of the wrong shape is ignored whole: no state change, no throw (main never sends one; this keeps the page safe if it ever does). */
function wellFormed(m: Record<string, unknown>, h: ShowHands): boolean {
  switch (m.action) {
    case "select-part": return typeof m.part === "string" && h.known.has(m.part);
    case "show-only": return strings(m.parts) && (!m.parts.length || m.parts.some((p) => h.known.has(p))) && (m.finding === undefined || (Number.isInteger(m.finding) && (m.finding as number) >= 0 && (m.finding as number) < h.cards));
    case "show-connections": return LENSES.includes(m.lens as string);
    case "show-all": case "open-graph": return true;
    default: return false;
  }
}

export function applyShow(msg: AssistantShow, h: ShowHands): void {
  if (typeof msg !== "object" || msg === null) return;
  const own = { ...msg } as Record<string, unknown>; // the message's OWN fields only: an inherited contract or action is ignored
  if (own.contract !== h.contract || !wellFormed(own, h)) return;
  const m = own as unknown as AssistantShow;
  if (m.action === "select-part") { setSelectedPartId(m.part); h.pick(m.part); }
  else if (m.action === "show-only") {
    // Only parts this window draws are kept, so the view is never left empty; an empty list shows everything.
    const kept = m.parts.filter((p) => h.drawn.has(p));
    setOnlyParts(kept);
    if (m.finding !== undefined) {
      // What a tap on "Only what this check uses" does for this finding (the Check screen opens it; the graph keeps its nodes).
      h.finding(m.finding);
      h.isolate(true);
      if (!kept.length) h.graph(true); // nothing narrows in 3D, so show the person the graph that did
    }
  } else if (m.action === "show-all") { setOnlyParts([]); h.isolate(false); }
  else if (m.action === "show-connections") { h.graph(false); h.design(); setDesignView({ lens: m.lens, hover: null }); }
  else h.graph(true);
}
