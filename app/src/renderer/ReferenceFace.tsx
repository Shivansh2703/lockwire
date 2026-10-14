import { useState, type ReactNode } from "react";

import type { Finding, ReferenceFace as ReferenceFacePayload } from "../shared/api.gen";
import { CommandLine, Disclosure, Field, FindingDetail, StatusPill } from "./kit";

export interface ReferenceFaceProps {
  payload: ReferenceFacePayload;
  selectedPlanFinding?: Finding | null;
}

type Row = { label: string; value: string };
type Block =
  | { t: "h"; n: number; text: string }
  | { t: "p"; text: string }
  | { t: "table"; head: string[]; rows: string[][] }
  | { t: "kv"; rows: Row[] };

const cells = (line: string) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

// A tiny reader for the engine's own markdown (headings, pipe tables, "- key: value" lists):
// it only lays the text out, it never reinterprets a value.
export function parseSheet(text: string): Block[] {
  const out: Block[] = [];
  const lines = text.split("\n");
  let parent = "";
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    const last = out[out.length - 1];
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    const li = /^(\s*)- (.*)$/.exec(line);
    if (!line.trim()) { parent = ""; continue; }
    if (h) out.push({ t: "h", n: h[1]!.length, text: h[2]! });
    else if (line.trim().startsWith("|")) {
      const head = cells(line);
      const rows: string[][] = [];
      i += 1; // the |---| separator
      while (lines[i + 1]?.trim().startsWith("|")) rows.push(cells(lines[++i]!));
      out.push({ t: "table", head, rows });
    } else if (li) {
      const nested = li[1]!.length > 0;
      const m = /^([^:]+):\s*(.*)$/.exec(li[2]!);
      const key = m ? m[1]! : li[2]!;
      const value = m ? m[2]! : "";
      if (!nested) parent = key;
      const row = { label: nested && parent ? `${parent} · ${key}` : key, value };
      if (last?.t === "kv" && lines[i - 1]?.trim()) last.rows.push(row); else out.push({ t: "kv", rows: [row] });
    } else if (last?.t === "kv" && lines[i - 1]?.trim()) {
      const r = last.rows[last.rows.length - 1]!;
      r.value = r.value ? `${r.value}\n${line.trim()}` : line.trim();
    } else if (last?.t === "p" && lines[i - 1]?.trim()) last.text += `\n${line}`;
    else out.push({ t: "p", text: line });
  }
  return out;
}

// Engine bookkeeping: not shown; the CLI chip above each sheet has the full text.
const BOOKKEEPING = new Set(["schema_version", "format_version", "id"]);
// Plain table headers: the engine's column names become words. Unlisted ones lose their
// underscores. The engine's own text is untouched; only the header cell is re-worded.
const HEADERS: Record<string, string> = {
  id: "Name", voltage: "Voltage", voltage_range: "Range", capacity: "Capacity", current_limit: "Limit",
  upstream: "Upstream", efficiency: "Eff.", charged_by: "Charged by",
};
const humanHeader = (h: string) => HEADERS[h] ?? (h.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()));
const EMPTY = new Set(["-", "—", ""]);

// "NOT SCORED — reason" -> a pill; the reason stays in the tooltip and for screen readers.
const NOT_SCORED = /^NOT SCORED\s*[—-]\s*([\s\S]*)$/;
function Val({ text }: { text: string }): ReactNode {
  const m = NOT_SCORED.exec(text);
  return m ? <span title={m[1]}><StatusPill status="muted">Not scored</StatusPill><span className="kit-sr"> {m[1]}</span></span> : text;
}

export function Sheet({ text }: { text: string }) {
  return (
    <div className="reference-sheet">
      {parseSheet(text).map((b, i): ReactNode => {
        if (b.t === "h") return <h4 key={i} className="reference-sheet-h" data-level={b.n}>{b.text}</h4>;
        if (b.t === "p") return <p key={i} className="reference-sheet-p"><Val text={b.text} /></p>;
        if (b.t === "kv") {
          const rows = b.rows.map((r) => ({ label: r.label, value: <span className="reference-sheet-v"><Val text={r.value} /></span> }));
          const shown = rows.filter((_, n) => !BOOKKEEPING.has(b.rows[n]!.label));
          return shown.length > 0 ? <FindingDetail key={i} rows={shown} /> : null;
        }
        const keep = b.head.map((_, j) => b.rows.some((r) => !EMPTY.has(r[j] ?? "")) || b.rows.length === 0);
        return (
          <div key={i} className="reference-table-wrap" tabIndex={0} role="region" aria-label="Table">
            <table className="reference-sheet-table">
              <thead><tr>{b.head.map((c, j) => keep[j] && <th key={j}>{humanHeader(c)}</th>)}</tr></thead>
              <tbody>{b.rows.map((r, j) => <tr key={j}>{r.map((c, k) => keep[k] && <td key={k} title={c}><Val text={c} /></td>)}</tr>)}</tbody>
            </table>
          </div>
        );
      })}
    </div>
  );
}

const middle = (s: string, max = 44) => (s.length <= max ? s : `${s.slice(0, 20)}…${s.slice(-(max - 21))}`);

/** Reference shows static material plus the explicitly selected served-plan evidence. */
export function ReferenceFace({ payload, selectedPlanFinding: f = null }: ReferenceFaceProps) {
  const [q, setQ] = useState("");
  const devices = payload.devices.filter((d) => d.id.toLowerCase().includes(q.trim().toLowerCase()));
  return (
    <section className="reference-face" aria-label="Docs">
      {f !== null && (
        <section className="reference-plan-finding" aria-label="Selected plan finding">
          <FindingDetail
            rows={[
              { label: "Quantity", value: <code>{f.quantity}</code> },
              { label: "Severity", value: <span className="reference-plan-finding-severity" data-severity={f.severity}>{f.severity}</span> },
              { label: "Observed", value: <span className="reference-plan-finding-observed">{f.observed}</span> },
              { label: "Expected", value: <span className="reference-plan-finding-expected">{f.expected}</span> },
              { label: "Fix", value: <span className="reference-plan-finding-action">{f.action}</span> },
              { label: "Source", value: f.source ?? "source unknown" },
              { label: "Witness", value: f.witness ?? "witness unknown" },
              { label: "Locked", value: <span className="reference-plan-finding-locked-at">{f.locked_at ?? "date unknown"}</span> },
              ...((f.alternatives ?? []).length ? [{
                label: "Instead",
                value: <ul>{(f.alternatives ?? []).map((a, i) => <li key={i} data-kind={a.kind}>{a.text} ({a.kind})</li>)}</ul>,
              }] : []),
            ]}
          />
        </section>
      )}
      <div className="reference-face-drawers">
        {payload.drawers.map((drawer, index) => (
          <div key={`${drawer.title}-${index}`} className="reference-drawer">
            {/* provenance is a tooltip: the sentence names where the sheet came from, not what it says */}
            <Disclosure summary={<span title={drawer.provenance}>{drawer.title}</span>}>
              <div className="reference-drawer-cli"><CommandLine command={drawer.cli_line} display={middle(drawer.cli_line)} /></div>
              <div className="reference-drawer-content"><Sheet text={drawer.content} /></div>
            </Disclosure>
          </div>
        ))}
      </div>
      {payload.devices.length > 0 && (
        <div className="reference-face-devices">
          <Disclosure summary={`Devices (${payload.devices.length})`}>
            <Field label="Search" aria-label="Search devices" placeholder="Search devices" value={q}
              onChange={(e: { target: { value: string } }) => setQ(e.target.value)} />
            <div className="reference-device-list">
              {devices.map((device) => (
                <div key={device.id} className="reference-device">
                  <Disclosure summary={device.id}><div className="reference-device-text"><Sheet text={device.text} /></div></Disclosure>
                </div>
              ))}
            </div>
          </Disclosure>
        </div>
      )}
    </section>
  );
}
