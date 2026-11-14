import { useState } from "react";

import type { PagePayload } from "../shared/api.gen";
import { Panel } from "./kit";
import { registerPanel } from "./panelRegistry";
import { useViewState } from "./scene/viewState";

type Device = NonNullable<PagePayload["wiring_devices"]>[number];
type Card = Device["connectors"][number]["card"];
type Wire = NonNullable<PagePayload["wiring_wires"]>[number];
const devicesOf = (p: PagePayload) => p.wiring_devices ?? []; // absent on a payload from before this field
const wiresOf = (p: PagePayload) => p.wiring_wires ?? [];

type PinFact = NonNullable<PagePayload["wiring_pins"]>[number];

/** Live ERC: the first branch of the engine's `check_voltage` (a driver's level above what the receiver takes),
 * read from the pin facts the engine put in the payload, tried both ways round. Returns one plain sentence or null. */
export function shortOf(facts: PinFact[], names: Map<string, string>, a: string, b: string): string | null {
  const f = new Map(facts.map((x) => [x.key, x]));
  for (const [d, r] of [[a, b], [b, a]] as const) {
    const drv = f.get(d), rx = f.get(r);
    if (!drv?.drives || !rx?.receives || drv.level_v == null || rx.level_v == null || rx.limit_v == null || drv.level_v <= rx.limit_v) continue;
    return rx.level_v === 0 ? `This would short ${drv.level_v} V from ${names.get(d)} to ground at ${names.get(r)}.` : `This puts ${drv.level_v} V from ${names.get(d)} onto ${names.get(r)}, which runs at ${rx.level_v} V.`;
  }
  return null;
}

const W = 180, ROW = 18, HEAD = 26, GAP = 80, PAD = 16;

/** Every pin joined to it by a wire, directly or through other wires, or by a shared net name. */
export function netOf(wires: Wire[], pin: string): Set<string> {
  const seen = new Set([pin]);
  for (let grew = true; grew;) {
    grew = false;
    for (const w of wires) {
      const hit = seen.has(w.from_pin) || seen.has(w.to_pin) || wires.some((o) => o.net && o.net === w.net && (seen.has(o.from_pin) || seen.has(o.to_pin)));
      for (const k of hit ? [w.from_pin, w.to_pin] : []) if (!seen.has(k)) { seen.add(k); grew = true; }
    }
  }
  return seen;
}

// ponytail: columns by wire direction (a device sits right of whatever feeds it), stacked in
// declared order, orthogonal edges with one bend. Fine to about a dozen devices; crossings are not
// minimised. Upgrade to a layered layout (dagre) if a robot outgrows that.
function layout(devices: Device[], wires: Wire[]) {
  const owner = new Map(devices.flatMap((d) => d.connectors.flatMap((c) => c.pins.map((p) => [p.key, d.id] as const))));
  const col = new Map(devices.map((d) => [d.id, 0]));
  const placed = new Set<string>(); // a device takes the column after the first wire that reaches it, so a loop cannot run away
  for (const w of wires) {
    const a = owner.get(w.from_pin), b = owner.get(w.to_pin);
    if (!a || !b || a === b) continue;
    placed.add(a);
    if (!placed.has(b)) { col.set(b, (col.get(a) as number) + 1); placed.add(b); }
  }
  const used = new Map<number, number>();
  const boxes = devices.map((d) => {
    const c = col.get(d.id) as number, rows = d.connectors.reduce((n, k) => n + 1 + k.pins.length, 0);
    const h = HEAD + rows * ROW + 8, y = used.get(c) ?? PAD;
    used.set(c, y + h + 16);
    return { d, c, x: PAD + c * (W + GAP), y, h };
  });
  const pos = new Map<string, { x: number; y: number; col: number }>();
  for (const b of boxes) {
    let y = b.y + HEAD;
    for (const k of b.d.connectors) { y += ROW; for (const p of k.pins) { pos.set(p.key, { x: b.x, y: y + ROW / 2, col: b.c }); y += ROW; } }
  }
  return { boxes, pos, width: PAD * 2 + (Math.max(...boxes.map((b) => b.c), 0) + 1) * (W + GAP) - GAP, height: Math.max(...used.values(), 0) + PAD };
}


type CardPinT = NonNullable<Card>[number];
const FIELDS = ["connector", "pin", "name", "aliases", "functions", "capabilities", "device file"];
const cell = (v: string) => (/[",\n;]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v);
// ponytail: list cells join with ";", so a name holding ";" would not round-trip. Quote-aware but not JSON.
/** The whole card as CSV, connectors and pins in the order the lockfile declares them. */
export function pinsCsv(d: Device): string {
  const rows = d.connectors.flatMap((k) => (k.card ?? []).map((p) => [k.name, p.number, p.name ?? "", (p.aliases ?? []).join(";"), (p.functions ?? []).join(";"), (p.capabilities ?? []).join(";"), p.os_device ?? ""]));
  return [FIELDS, ...rows].map((r) => r.map(cell).join(",")).join("\n") + "\n";
}
const matches = (p: CardPinT, q: string) => [p.number, p.name ?? "", p.os_device ?? "", ...(p.aliases ?? []), ...(p.functions ?? []), ...(p.capabilities ?? [])].some((t) => t.toLowerCase().includes(q));
const hasPins = (d: Device) => d.connectors.some((k) => (k.card ?? []).length > 0);

function PinCard({ device, onBack }: { device: Device; onBack: () => void }) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const save = () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([pinsCsv(device)], { type: "text/csv" }));
    a.download = `${device.id}-pins.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };
  const shown = device.connectors.map((k) => ({ k, pins: (k.card ?? []).filter((p) => !q || matches(p, q)) })).filter((x) => (x.k.card ?? []).length > 0);
  return (
    <div className="pin-card">
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8, flexWrap: "wrap" }}>
        <button type="button" onClick={onBack}>Back to diagram</button>
        <strong style={{ font: "600 14px var(--sans)" }}>{device.name}</strong>
        {hasPins(device) && <input type="search" aria-label="Search pins" placeholder="Search pins or other names" value={query} onChange={(e) => setQuery(e.target.value)} style={{ flex: 1, minWidth: 140 }} />}
        {hasPins(device) && <button type="button" onClick={save}>Export CSV</button>}
      </div>
      {!hasPins(device) && <div className="assistant-note">This robot has not listed any pins for {device.name} yet. Pins come from the robot file.</div>}
      {hasPins(device) && q && shown.every((x) => x.pins.length === 0) && <div className="assistant-note">No pin matches &ldquo;{query.trim()}&rdquo;.</div>}
      <div style={{ overflow: "auto", maxHeight: "70vh" }}>
        {shown.filter((x) => x.pins.length > 0).map(({ k, pins }) => (
          <table key={k.name} style={{ width: "100%", borderCollapse: "collapse", font: "400 12px var(--sans)", marginBottom: 12 }}>
            <caption style={{ textAlign: "left", font: "600 13px var(--sans)", padding: "4px 0" }}>{k.name || "Pins"}{k.type ? ` (${k.type})` : ""}</caption>
            <thead><tr style={{ textAlign: "left", color: "var(--muted)" }}>{["Pin", "Name", "Also called", "Other functions", "Part file"].map((h) => <th key={h} scope="col" style={{ padding: "2px 8px 2px 0" }}>{h}</th>)}</tr></thead>
            <tbody>
              {pins.map((p) => (
                <tr key={p.number} style={{ borderTop: "1px solid var(--line)" }}>
                  <td style={{ padding: "2px 8px 2px 0" }}>{p.number}</td>
                  <td>{p.name ?? ""}</td>
                  <td>{(p.aliases ?? []).join(", ")}</td>
                  <td>{[...(p.functions ?? []), ...(p.capabilities ?? [])].join(", ")}</td>
                  <td>{p.os_device ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
      </div>
    </div>
  );
}

/** Writes one wire from pin A to pin B (the engine's `add wire`), reloads the robot, returns the engine's refusal or null. */
export type AddWire = (id: string, from: string, to: string) => Promise<string | null>;
const slug = (id: string) => id.replace(/^dev\./, "");

export function WiringPanel({ page, onAddWire }: { page: PagePayload; onAddWire?: AddWire }) {
  const [shown, setShown] = useState<string | null>(null);
  const [card, setCard] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null); // pin A, waiting for pin B
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<string | null>(null); // pin B whose wire the engine's rule flagged, waiting for a second click
  const [saved, setSaved] = useState(false);
  const devices = devicesOf(page), wires = wiresOf(page);
  const compare = useViewState().compare; // TEAM-02: the Pro version diff, or null
  const deviceOf = (key: string) => key.split("|")[0]!;
  const names = new Map(devices.flatMap((d) => d.connectors.flatMap((k) => k.pins.map((p) => [p.key, `${d.name} ${p.label}`] as const))));
  const cancel = () => { setPicked(null); setPending(null); };
  const pick = async (key: string, confirmed = false) => {
    if (!onAddWire || busy) return;
    setPending(null);
    if (!picked) { setPicked(key); setNote(null); return; }
    if (picked === key) { cancel(); return; }
    if (deviceOf(picked) === deviceOf(key)) { setNote("Both pins are on the same part. Pick a pin on a different part."); return; }
    if (wires.some((w) => (w.from_pin === picked && w.to_pin === key) || (w.from_pin === key && w.to_pin === picked))) { setNote("These two pins are already wired together."); setPicked(null); return; }
    if (!confirmed && shortOf(page.wiring_pins ?? [], names, picked, key)) { setPending(key); return; }
    const base = `wire.${slug(deviceOf(picked))}-${slug(deviceOf(key))}`;
    let id = base;
    for (let n = 2; wires.some((w) => w.id === id); n++) id = `${base}-${n}`;
    setBusy(true);
    const refusal = await onAddWire(id, picked, key);
    setBusy(false);
    setPicked(null);
    setNote(refusal);
    setSaved(refusal === null);
  };
  const { boxes, pos, width, height } = layout(devices, wires);
  const net = shown ? netOf(wires, shown) : null;
  // A pin's dot sits on the side facing its first wire's other end.
  const side = (key: string) => {
    const me = pos.get(key)!, w = wires.find((o) => o.from_pin === key || o.to_pin === key);
    const other = w && pos.get(w.from_pin === key ? w.to_pin : w.from_pin);
    return other && other.col < me.col ? "left" : "right";
  };
  const dot = (key: string) => { const p = pos.get(key)!; return { x: p.x + (side(key) === "left" ? 0 : W), y: p.y }; };
  const route = (w: Wire, i: number) => {
    const a = dot(w.from_pin), b = dot(w.to_pin), bend = (i % 5) * 6;
    const out = side(w.from_pin) === "right" ? 1 : -1, into = side(w.to_pin) === "left" ? -1 : 1;
    if (out === 1 && into === -1 && b.x > a.x) { const mid = a.x + GAP / 2 + bend - 12; return `M${a.x},${a.y} H${mid} V${b.y} H${b.x}`; }
    const far = Math.max(a.x, b.x) + 20 + bend; // same column or backwards: go round the right side
    return `M${a.x},${a.y} H${far} V${b.y} H${b.x}`;
  };
  const added = new Set(compare?.wires.added);
  const drawable = (compare?.wires.removed ?? []).filter((w) => pos.has(w.from_pin) && pos.has(w.to_pin));
  const undrawn = (compare?.wires.removed ?? []).filter((w) => !drawable.includes(w));
  const lit = (w: Wire) => !net || (net.has(w.from_pin) && net.has(w.to_pin));
  const liveKey = pending ?? (picked && shown && shown !== picked ? shown : null);
  const warn = picked && liveKey ? shortOf(page.wiring_pins ?? [], names, picked, liveKey) : null;
  const findings = page.wiring_findings ?? [];
  const open = devices.find((d) => d.id === card);
  if (open) return <Panel title="Wiring"><PinCard device={open} onBack={() => setCard(null)} /></Panel>;
  return (
    <Panel title="Wiring">
      <div className="wiring-legend" style={{ display: "flex", gap: 16, font: "400 13px var(--sans)", marginBottom: 8 }}>
        <span><span aria-hidden style={{ display: "inline-block", width: 22, borderTop: "3px solid var(--accent)", verticalAlign: "middle", marginRight: 6 }} />Power wire</span>
        <span><span aria-hidden style={{ display: "inline-block", width: 22, borderTop: "2px dashed var(--ink)", verticalAlign: "middle", marginRight: 6 }} />Signal wire</span>
        {compare && <span className="wiring-diff-added"><span aria-hidden style={{ display: "inline-block", width: 22, borderTop: "4px solid var(--diff-added)", verticalAlign: "middle", marginRight: 6 }} />Added since that version ({compare.wires.added.length})</span>}
        {compare && <span className="wiring-diff-removed"><span aria-hidden style={{ display: "inline-block", width: 22, borderTop: "4px dotted var(--diff-removed)", verticalAlign: "middle", marginRight: 6 }} />Removed since that version ({compare.wires.removed.length})</span>}
      </div>
      {warn && (
        <div className="assistant-note wiring-short" role="alert" style={{ borderLeft: "3px solid var(--bad, #c0392b)", paddingLeft: 8 }}>
          {warn}
          {pending && <span style={{ display: "flex", gap: 8, marginTop: 6 }}>
            <button type="button" onClick={() => void pick(pending, true)}>Wire it anyway</button>
            <button type="button" onClick={cancel}>Do not wire</button>
          </span>}
        </div>
      )}
      <div style={{ display: "flex", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>
      <div style={{ overflow: "auto", maxHeight: "70vh", flex: "1 1 420px", minWidth: 0 }} onKeyDown={(e) => { if (e.key === "Escape" && picked) { e.stopPropagation(); cancel(); } }}>
        <svg className="wiring-diagram" role="img" aria-label="Wiring diagram" width="100%" viewBox={`0 0 ${width + 40} ${height}`} style={{ height: "auto", font: "400 12px var(--sans)", minWidth: (width + 40) * 0.7 }}>
          {wires.map((w, i) => (
            <path key={w.id} className={w.rail ? "wiring-rail" : "wiring-signal"} data-wire={w.id} data-kind={w.rail ? "rail" : "signal"} d={route(w, i)} fill="none"
              data-diff={added.has(w.id) ? "added" : undefined}
              stroke={added.has(w.id) ? "var(--diff-added)" : w.rail ? "var(--accent)" : "var(--ink)"} strokeWidth={added.has(w.id) ? 4 : w.rail ? 3 : 2} strokeDasharray={w.rail ? undefined : "5 3"} opacity={lit(w) ? 1 : 0.15}>
              <title>{(added.has(w.id) ? "Added since that version. " : "") + (w.rail_label ? `${w.name}, carries ${w.rail_label}` : `${w.name}, a signal wire`)}</title>
            </path>
          ))}
          {drawable.map((w, i) => (
            <path key={`removed:${w.id}`} className="wiring-removed" data-wire={w.id} data-diff="removed" d={route(w, i)} fill="none" stroke="var(--diff-removed)" strokeWidth={4} strokeDasharray="1 6" strokeLinecap="round" opacity={lit(w) ? 1 : 0.15}>
              <title>{`${w.name}, removed since that version`}</title>
            </path>
          ))}
          {boxes.map(({ d, x, y, h }) => {
            let ty = y + HEAD;
            return (
              <g key={d.id} className="wiring-device" data-device={d.id} style={{ cursor: "pointer" }} onClick={() => setCard(d.id)}>
                <rect x={x} y={y} width={W} height={h} rx={8} fill="var(--surface)" stroke="var(--line)" />
                <text x={x + 10} y={y + 17} fill="var(--ink)" tabIndex={0} role="button" aria-label={`Open pin card for ${d.name}`} onKeyDown={(e) => e.key === "Enter" && setCard(d.id)} style={{ font: "600 13px var(--sans)" }}>{d.name.length > 21 ? `${d.name.slice(0, 20)}…` : d.name}<title>{d.name}</title></text>
                {d.connectors.map((k) => {
                  const head = ty + 13; ty += ROW + k.pins.length * ROW;
                  return (
                    <g key={k.name}>
                      <text x={x + 10} y={head} fill="var(--muted)">{k.name || "Pins"}</text>
                      {k.pins.map((p) => {
                        const at = pos.get(p.key)!, on = !!net && net.has(p.key), left = side(p.key) === "left";
                        return (
                          <g key={p.key} className="wiring-pin" data-pin={p.key} tabIndex={0} aria-label={`${d.name} ${k.name} pin ${p.label}`}
                            onMouseEnter={() => setShown(p.key)} onMouseLeave={() => setShown(null)} onFocus={() => setShown(p.key)} onBlur={() => setShown(null)}
                            data-picked={picked === p.key ? "true" : undefined} style={onAddWire ? { cursor: "crosshair" } : undefined}
                            onClick={onAddWire ? (e) => { e.stopPropagation(); void pick(p.key); } : undefined}
                            onKeyDown={onAddWire ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); void pick(p.key); } } : undefined}>
                            <rect x={x + 1} y={at.y - ROW / 2} width={W - 2} height={ROW} fill={on || picked === p.key ? "var(--field)" : "transparent"} stroke={picked === p.key ? "var(--accent)" : undefined} />
                            <text x={x + (left ? 14 : 10) + 8} y={at.y + 4} fill="var(--ink)">{p.label}</text>
                            <circle cx={x + (left ? 0 : W)} cy={at.y} r={4} fill={on ? "var(--accent)" : "var(--surface)"} stroke="var(--ink)" />
                            <title>{`${d.name}, ${k.name || "pin"} ${p.label}`}</title>
                          </g>
                        );
                      })}
                    </g>
                  );
                })}
              </g>
            );
          })}
        </svg>
      </div>
      {(findings.length > 0 || saved) && (
        <div className="wiring-findings" style={{ flex: "0 1 260px", font: "400 13px var(--sans)" }}>
          <strong style={{ font: "600 13px var(--sans)" }}>Pin checks</strong>
          {findings.length === 0 && <div className="assistant-note">Checked again after saving. No pin findings.</div>}
          {findings.map((f, i) => <div key={i} className="assistant-note" data-wire-finding={f.wire}><strong>{f.wire_name}</strong>: {f.problem}. Expected {f.limit}.</div>)}
        </div>
      )}
      </div>
      {undrawn.length > 0 && <div className="assistant-note wiring-undrawn">Removed, and not drawn because a device on them is gone: {undrawn.map((w) => w.name).join(", ")}.</div>}
      {note && <div className="assistant-note" role="alert">{note}</div>}
      <div className="assistant-note" role="status" style={{ minHeight: 20 }}>{busy ? "Saving the wire." : picked ? "Now click the pin it goes to. Esc cancels." : net ? `${net.size} pins on this net.` : onAddWire ? "Click one pin, then another, to wire them. Click a part name to see its pins." : "Point at a pin to light up everything wired to it. Click a part to see its pins."}</div>
    </Panel>
  );
}

registerPanel({ id: "wiring", label: "Wiring", hasData: (p) => devicesOf(p).some((d) => d.connectors.some((k) => k.pins.length > 0)), Body: ({ page, onAddWire }) => <WiringPanel page={page} onAddWire={onAddWire} /> });
