import { useEffect, useState } from "react";

import type { SwapKnockOn, SwapPreviewPayload } from "../shared/api.gen";
import { Button, EmptyState, Notice, Panel, StatusPill } from "./kit";

// Part Swap: the catalog servos for one joint, grouped fit / tight / too weak by its
// real gravity load; picking one shows the joints above it, and "Use this" writes it against the
// revision the app opened. One panel, opened from a servo finding (Check) or a joint (Design).
const GROUPS = [["fit", "Fit", "go"], ["tight", "Tight", "warn"], ["too weak", "Too weak", "bad"]] as const;
const times = (r: number) => (r >= 100 ? "100x+" : `${r.toFixed(1)}x`);
const nm = (x: number) => `${x.toFixed(2)} N·m`;

function knockLine(k: SwapKnockOn): string {
  if (k.before_nm === null || k.after_nm === null) return `${k.joint}: load not scored`;
  const loads = `${k.joint} ${nm(k.before_nm)} → ${nm(k.after_nm)}`;
  if (k.status === null) return loads;
  return k.dropped ? `warning: ${loads}, now ${k.status} (${times(k.ratio!)}), was ${k.was}` : `${loads}, still ${k.status} (${times(k.ratio!)})`;
}

export function SwapPanel({ contract, revision, joint, onClose, onApplied }: {
  contract: string;
  revision: string;
  joint: string;
  onClose: () => void;
  /** The swap is written: the caller re-checks the robot and shows the engine's lines. */
  onApplied: (lines: string[]) => void;
}) {
  const [list, setList] = useState<SwapPreviewPayload | null>(null);
  const [shown, setShown] = useState<SwapPreviewPayload | null>(null); // the knock-on for the picked row
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const fail = (e: unknown) => setError(e instanceof Error ? e.message : String(e));
  useEffect(() => {
    window.lockwire.swapPreview({ contract, joint }).then((p) => { setList(p); setShown(p); }, fail);
  }, [contract, joint]);
  // The knock-on is computed for one candidate at a time (swap.preview): ask again for a new pick.
  const pick = (id: string) => { setError(null); void window.lockwire.swapPreview({ contract, joint, part_id: id }).then(setShown, fail); };
  const use = async (id: string) => {
    setBusy(true);
    setError(null);
    try {
      const reply = (await window.lockwire.writeContract({ command: "swap-apply", args: { contract, joint, part_id: id, expected_revision: revision } })) as { refusal?: string | null; payload?: { lines?: string[] } };
      if (reply.refusal) setError(reply.refusal);
      else onApplied(reply.payload?.lines ?? []);
    } catch (e) { fail(e); } finally { setBusy(false); }
  };
  const chosen = shown?.chosen ?? null;
  const weak = shown ? [...(shown.candidates.find((c) => c.id === chosen)?.status === "too weak" ? [joint] : []), ...shown.knock_on.filter((k) => k.dropped && k.status === "too weak").map((k) => k.joint)] : [];
  return (
    <Panel title={`Swap the ${joint} servo`} actions={<Button variant="quiet" onClick={onClose}>Back</Button>}
      footer={chosen && <Button variant="primary" disabled={busy} onClick={() => void use(chosen)}>Use this</Button>}>
      {list && <p className="check-note">Now {list.current ?? "no servo declared"}, gravity load {nm(list.load_nm)}. Fit means the stall torque is at least twice the load.</p>}
      {!list && !error && <p className="check-note">Ranking servos…</p>}
      {shown && shown.notes.length > 0 && <ul className="swap-knock" aria-label="Notes">{shown.notes.map((n) => <li key={n} className="swap-note">{n}</li>)}</ul>}
      {weak.length > 0 && <Notice kind="error">Warning: {weak.join(" and ")} would be too weak with this servo. Pick a stronger one, or expect it to fail under load.</Notice>}
      {error && <Notice kind="error">{error}</Notice>}
      {list && list.candidates.some((c) => c.flags.includes("different bus")) && <p className="check-note">"Different bus" means the servo needs a different cable or adapter, and a different protocol, than the current servo.</p>}
      {list && list.candidates.length === 0 && <EmptyState title="no cited alternatives yet" />}
      {list && GROUPS.map(([status, word, pill]) => {
        const rows = list.candidates.filter((c) => c.status === status);
        return rows.length > 0 && (
          <section key={status} className="swap-group">
            <h3 className="check-section"><StatusPill status={pill}>{word}</StatusPill></h3>
            {rows.map((c) => (
              <div key={c.id}>
                <button type="button" className="swap-row" aria-pressed={chosen === c.id} onClick={() => pick(c.id)}>
                  <span className="swap-name">{c.name}</span>
                  <span className="swap-label">{c.label}</span>
                  <span className="swap-nums">{nm(c.stall_nm)} stall · {times(c.ratio)} · {c.mass_g} g</span>
                  {c.flags.length > 0 && <span className="swap-flags">{c.flags.join(", ")}</span>}
                </button>
                {chosen === c.id && shown && (
                  <ul className="swap-knock" aria-label="Joints above">
                    <li>{shown.delta_g >= 0 ? "+" : ""}{shown.delta_g} g on the {joint} link</li>
                    {shown.knock_on.map((k) => <li key={k.joint} className={k.dropped ? "swap-drop" : undefined}>{knockLine(k)}</li>)}
                  </ul>
                )}
              </div>
            ))}
          </section>
        );
      })}
    </Panel>
  );
}
