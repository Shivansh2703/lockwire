import type { PagePayload } from "../shared/api.gen";
import { Panel } from "./kit";
import { registerPanel } from "./panelRegistry";
import { findInterference, sweepJoints, type Finding, type SweepResult } from "./scene/collide";
import { setSelectedGroup } from "./scene/selectionStore";
import { lenNum, lenUnit, sweepNum, sweepUnit, useUnits } from "./units";

const cm3 = (m3: number) => (m3 * 1e6).toFixed(m3 * 1e6 < 10 ? 2 : 1);
const mm = (m: number) => lenNum(m * 1000, true);

/** One sentence per pair, with how sure the number is. */
export function say(f: Finding): string {
  const names = `${f.a} and ${f.b}`;
  const note = f.approximate ? " (approximate: a cylinder is checked as its bounding box)" : "";
  if (f.overlap) return `${names} overlap by ${f.volumeExact ? "" : "about "}${cm3(f.volume!)} cm³${f.volumeExact ? "" : " (estimate)"}${note}`;
  return `${names} are ${f.gapExact ? "" : "at least "}${mm(f.gap!)} ${lenUnit()} apart${note}`;
}

/** One sentence per swept joint: where it first hits, or how close it comes. */
export function saySweep(s: SweepResult): string {
  const u = ` ${sweepUnit(s.unit)}`;
  if (s.hit) return `${s.hit.a === s.joint ? "" : `${s.joint} joint: `}${s.hit.a} hits ${s.hit.b} at ${sweepNum(s.hit.at, s.unit)}${u}`;
  return s.closest ? `${s.joint}: clear through its range, closest to ${s.closest.b} is ${mm(s.closest.gap)} ${lenUnit()} at ${sweepNum(s.closest.at, s.unit)}${u}` : `${s.joint}: no shaped parts to check`;
}

/** What the sweep did and how far to trust a hit angle. */
export function sweepNote(swept: SweepResult[]): string {
  const steps = swept.map((s) => `${s.joint} ${sweepNum(s.step, s.unit, true)} ${sweepUnit(s.unit)}`).join(", ");
  return `Each joint is moved alone in 36 steps from its lower to its upper limit, the others at rest${steps ? `. Step size: ${steps}` : ""}. A hit is shown at the first step that touches, and a thin contact between steps can be missed.`;
}

export function InterferencePanel({ page }: { page: PagePayload }) {
  useUnits();
  const r = findInterference(page);
  const sw = sweepJoints(page);
  const row = (f: Finding, bad: boolean) => (
    <button key={f.a + f.b} type="button" className="interference-row" onClick={() => setSelectedGroup({ id: "interference", members: [f.a, f.b] })} style={{ display: "block", textAlign: "left", background: "none", border: 0, padding: "4px 0", font: "400 13px var(--sans)", color: bad ? "var(--warn)" : "var(--ink)", cursor: "pointer" }}>
      {say(f)}
    </button>
  );
  return (
    <Panel title="Overlaps and clearance">
      {r.overlaps.length + r.close.length === 0 && !sw.swept.some((s) => s.hit) && <p>No overlapping or tight parts found.</p>}
      {r.overlaps.map((f) => row(f, true))}
      {r.close.map((f) => row(f, false))}
      {r.ignored.length > 0 && (
        <p className="assistant-note">Ignored pairs, left out on purpose: {r.ignored.map((p) => `${p.link1} and ${p.link2} (${p.reason})`).join("; ")}.</p>
      )}
      {sw.swept.length + sw.notSwept.length > 0 && (
        <>
          <h4 style={{ margin: "12px 0 4px", font: "600 13px var(--sans)" }}>Through the joint range</h4>
          {sw.swept.map((s) => <p key={s.joint} style={{ margin: "4px 0", font: "400 13px var(--sans)", color: s.hit ? "var(--warn)" : "var(--ink)" }}>{saySweep(s)}</p>)}
          {sw.notSwept.length > 0 && <p className="assistant-note">Not swept, no limits declared: {sw.notSwept.join(", ")}.</p>}
          <p className="assistant-note">{sweepNote(sw.swept)}</p>
        </>
      )}
      <p className="assistant-note">Parts drawn from mesh files are not checked. Pairs joined by a joint are not checked. Tight means under {lenNum(5)} {lenUnit()} (rule of thumb).</p>
    </Panel>
  );
}

registerPanel({ id: "interference", label: "Overlaps", hasData: (p) => (p.links ?? []).some((l) => (l.solids ?? []).length > 0), Body: ({ page }) => <InterferencePanel page={page} /> });
