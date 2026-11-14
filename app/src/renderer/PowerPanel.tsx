import { useState } from "react";

import type { PagePayload } from "../shared/api.gen";
import { Button, Field, Notice, Panel } from "./kit";
import { registerPanel } from "./panelRegistry";
import { pro } from "./proRegistry";

type Stage = NonNullable<PagePayload["power_stages"]>[number];
const stagesOf = (p: PagePayload) => p.power_stages ?? []; // absent on a payload from before this field

type Column = NonNullable<PagePayload["power_columns"]>[number];
type Stress = NonNullable<PagePayload["power_stress"]>[number];
type Pack = NonNullable<PagePayload["power_pack"]>;

const n = (v: number, d = 1) => Number(v.toFixed(d)).toString();
const volts = (v: number | null) => (v === null ? "unknown" : `${n(v)} V`);

/** One stage: its voltage in and out, the current it carries, its efficiency and the heat it loses. */
function StageCard({ s, shown, onShow, onDeclare }: { s: Stage; shown: boolean; onShow: () => void; onDeclare?: (path: string, value: string, source: string) => Promise<string | null> }) {
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const save = async () => {
    const pct = Number(text);
    if (!text.trim() || !Number.isFinite(pct) || pct < 1 || pct > 100) return setError("Type a percent from 1 to 100.");
    setError("");
    const refused = await onDeclare!(`topology/rails/${s.rail}/efficiency`, String(pct / 100), "typed in the Power panel");
    if (refused) setError(refused); else setText("");
  };
  return (
    <div className="power-stage" tabIndex={0} aria-label={s.label} onMouseEnter={onShow} onFocus={onShow}
      style={{ flex: "0 0 168px", display: "flex", flexDirection: "column", gap: 4, padding: 10, border: `1px solid ${shown ? "var(--accent)" : "var(--line)"}`, borderRadius: "var(--radius-sm)", background: "var(--surface)", font: "400 13px var(--sans)" }}>
      <strong>{s.label}</strong>
      <span>{volts(s.volts_in)} in, {volts(s.volts_out)} out</span>
      <span>{s.amps_out === null ? "Current unknown" : `${n(s.amps_out, 2)} A out`}</span>
      <span>Efficiency {n(s.efficiency * 100)} %</span>
      <span style={{ font: "600 15px var(--sans)", color: s.watts_lost > 0 ? "var(--warn)" : "var(--ink)" }}>{n(s.watts_lost, 2)} W lost as heat</span>
      <span className="assistant-note">{n(s.watts_in, 2)} W in, {n(s.watts_out, 2)} W out</span>
      {s.fed_by === null ? <span className="assistant-note">Not fed from the battery or another rail, so no loss is counted here.</span>
        : onDeclare && <>
          <Field label="Efficiency (%)" aria-label={`efficiency for ${s.label}`} value={text} placeholder={n(s.efficiency * 100)} onChange={(e: { target: { value: string } }) => setText(e.target.value)} error={error || undefined} />
          <Button variant="quiet" disabled={!text.trim()} onClick={() => void save()}>Save efficiency</Button>
        </>}
    </div>
  );
}

const section = { font: "400 13px var(--sans)", marginTop: 8, marginBottom: 8 } as const;
const cell = { textAlign: "right", padding: "2px 8px" } as const;

/** Load per rail in watts at three operating points; worst case is the highest draw each part declares. */
function Columns({ rows }: { rows: Column[] }) {
  return (
    <details open className="power-columns" style={section}>
      <summary><strong>Load on each rail</strong></summary>
      <table style={{ borderCollapse: "collapse" }}>
        <thead><tr><th style={{ ...cell, textAlign: "left" }}>Rail</th><th style={cell}>Light</th><th style={cell}>Typical</th><th style={cell}>Worst case</th></tr></thead>
        <tbody>{rows.map((c) => <tr key={c.rail}><td style={{ ...cell, textAlign: "left" }}>{c.label}</td><td style={cell}>{n(c.light_w, 2)} W</td><td style={cell}>{n(c.typical_w, 2)} W</td><td style={cell}>{n(c.max_w, 2)} W</td></tr>)}</tbody>
      </table>
      {rows.some((c) => c.typical_from_plan) && <span className="assistant-note">Typical follows your plan. </span>}
      <span className="assistant-note">Light is the lowest draw a part declares, worst case the highest. Parts with no draw written down are left out.</span>
    </details>
  );
}

/** Every part against its rating, fullest first. The 80 percent line is advice with no source yet. */
function StressList({ rows }: { rows: Stress[] }) {
  return (
    <details open={rows.some((r) => r.over_advice)} className="power-stress" style={section}>
      <summary><strong>Parts against their ratings</strong></summary>
      {rows.map((r, i) => (
        <div key={i} style={{ color: r.pct > 100 ? "var(--warn)" : "var(--ink)" }}>
          {r.part}, {r.quantity}: {n(r.value, 2)} of {n(r.rating, 2)} {r.unit} ({n(r.pct, 0)} % of rating)
          {r.pct > 100 ? <> (rating: {r.source})</> : r.over_advice ? <span className="assistant-note"> Over 80 % leaves little margin (advice: not yet sourced).</span> : null}
        </div>
      ))}
    </details>
  );
}

/** The declared pack and what is left after the reserve; the plan's need when a plan with timings exists. */
function PackSection({ pack }: { pack: Pack }) {
  const short = pack.plan_need_wh !== null && pack.plan_need_wh > pack.usable_wh;
  return (
    <details className="power-pack" style={section}>
      <summary><strong>Battery pack</strong></summary>
      <div>{pack.voltage_v === null ? "Voltage not declared" : `${n(pack.voltage_v)} V`}, {n(pack.capacity_wh)} Wh, reserve {pack.reserve_pct === null ? "not set" : `${n(pack.reserve_pct)} %`}</div>
      <div>{n(pack.usable_wh)} Wh usable after the reserve</div>
      {pack.plan_need_wh !== null && <div style={{ color: short ? "var(--warn)" : "var(--ink)" }}>The plan needs {n(pack.plan_need_wh)} Wh{short ? ", more than the pack gives" : ""}</div>}
    </details>
  );
}

export function PowerPanel({ page, onDeclare, contract, revision }: { page: PagePayload; contract?: string; revision?: string; onDeclare?: (path: string, value: string, source: string) => Promise<string | null> }) {
  const [shown, setShown] = useState<string | null>(null);
  const hovered = stagesOf(page).find((s) => s.rail === shown);
  return (
    <Panel title="Power">
      <div className="power-stages" style={{ display: "flex", gap: 8, overflowX: "auto", paddingBottom: 8 }}>
        {stagesOf(page).map((s) => <StageCard key={s.rail} s={s} shown={shown === s.rail} onShow={() => setShown(s.rail)} onDeclare={onDeclare} />)}
      </div>
      <div className="power-parts" role="status" style={{ font: "400 13px var(--sans)", minHeight: 40 }}>
        {hovered ? <>
          <strong>On {hovered.label}</strong>
          {hovered.loads.map((l) => <div key={l.id}>{l.id}: {l.watts === null ? "draw not declared" : `${n(l.watts, 2)} W`}</div>)}
          {hovered.loads.length === 0 && <div>No parts draw from it.</div>}
          {hovered.unknown_loads > 0 && <Notice kind="info">{hovered.unknown_loads} part{hovered.unknown_loads === 1 ? " has" : "s have"} no draw written down, so the totals leave {hovered.unknown_loads === 1 ? "it" : "them"} out.</Notice>}
        </> : <span className="assistant-note">Point at a stage to see the parts on it.</span>}
      </div>
      {(page.power_columns ?? []).length > 0 && <Columns rows={page.power_columns ?? []} />}
      {(page.power_stress ?? []).length > 0 && <StressList rows={page.power_stress ?? []} />}
      {page.power_pack && <PackSection pack={page.power_pack} />}
      {pro?.PowerMore && <pro.PowerMore page={page} contract={contract} revision={revision} onDeclare={onDeclare} />}
    </Panel>
  );
}

registerPanel({ id: "power", label: "Power", hasData: (p) => stagesOf(p).length > 0, Body: ({ page, onDeclare, contract, revision }) => <PowerPanel page={page} onDeclare={onDeclare} contract={contract} revision={revision} /> });
