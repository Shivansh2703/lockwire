import { useState, type ReactNode } from "react";

import type { Finding, ReferenceFace as ReferenceFacePayload } from "../shared/api.gen";
import { tidyNumbers } from "./kit/format";
import { linkTo } from "./FindingList";
import { insteadJump, type Jump, type OnJump } from "./jump";
import { CommandLine, Disclosure, Field, FindingDetail, Jumper, StatusPill } from "./kit";

export interface ReferenceFaceProps {
  payload: ReferenceFacePayload;
  selectedPlanFinding?: Finding | null;
  /** Names the robot knows (part ids, group labels): a sheet cell holding one selects it. */
  names?: Set<string>;
  onJump?: OnJump;
}

/** A sheet's word that names a part, rail or device acts: it lights that thing on the robot. */
type Named = { names?: Set<string>; onJump?: OnJump };
const named = (text: string, { names, onJump }: Named, node: ReactNode): ReactNode =>
  onJump && names?.has(text) && onJump.can({ name: text }) ? <Jumper label={`Show ${text}`} onClick={() => onJump({ name: text })}>{node}</Jumper> : node;

type Row = { label: string; value: string };
type Block =
  | { t: "h"; n: number; text: string }
  | { t: "p"; text: string }
  | { t: "table"; head: string[]; rows: string[][] }
  | { t: "kv"; rows: Row[] }
  | { t: "tree"; rows: { depth: number; text: string }[] };

const cells = (line: string) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

// A tiny reader for the engine's own markdown (headings, pipe tables, "- key: value" lists):
// it only lays the text out, it never reinterprets a value.
export function parseSheet(text: string): Block[] {
  const out: Block[] = [];
  const lines = text.split("\n");
  let parent = "";
  let heading = "";
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]!;
    const last = out[out.length - 1];
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    const li = /^(\s*)- (.*)$/.exec(line);
    if (!line.trim()) { parent = ""; continue; }
    if (h) { heading = h[2]!; out.push({ t: "h", n: h[1]!.length, text: heading }); }
    else if (heading === "Power tree") {
      // Battery, then rail, then load: depth is the engine's own two-space indent.
      const row = { depth: (/^ */.exec(line)![0].length) / 2, text: line.trim().replace(/^- /, "") };
      if (last?.t === "tree") last.rows.push(row); else out.push({ t: "tree", rows: [row] });
    }
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
function Val({ text, why }: { text: string; why?: boolean }): ReactNode {
  text = tidyNumbers(text);
  const m = NOT_SCORED.exec(text);
  return m ? <span title={m[1]}><StatusPill status="muted">Not scored</StatusPill><span className={why && !/Engineer report/.test(m[1]!) ? "reference-why" : "kit-sr"}> {m[1]}</span></span> : text;
}

type TreeRow = { depth: number; text: string };
// A branch whose direct children are all leaves and number more than 6 start folded (the humanoid's 12 servos per rail).
const FOLD = 6;
function Tree({ rows, nm }: { rows: TreeRow[]; nm: Named }) {
  const [open, setOpen] = useState<Set<number>>(new Set());
  const kids = (i: number) => { const out: number[] = []; for (let j = i + 1; j < rows.length && rows[j]!.depth > rows[i]!.depth; j++) out.push(j); return out; };
  const foldable = (i: number) => { const k = kids(i); return k.length > FOLD && k.every((j) => rows[j]!.depth === rows[i]!.depth + 1 && kids(j).length === 0); };
  const shown: ReactNode[] = [];
  for (let i = 0; i < rows.length; i++) {
    const at = i, r = rows[i]!, text = tidyNumbers(r.text), fold = foldable(i);
    shown.push(
      <li key={i} style={{ paddingLeft: r.depth * 16 }}>
        {named(text, nm, text)}
        {fold && <button type="button" className="reference-tree-fold" aria-expanded={open.has(at)} onClick={() => setOpen((o) => { const n = new Set(o); if (!n.delete(at)) n.add(at); return n; })}>{open.has(at) ? "Hide" : `Show ${kids(i).length} more`}</button>}
      </li>,
    );
    if (fold && !open.has(at)) i += kids(i).length;
  }
  return <ul className="reference-tree">{shown}</ul>;
}

export function Sheet({ text, ...nm }: { text: string } & Named) {
  return (
    <div className="reference-sheet">
      {parseSheet(text).map((b, i): ReactNode => {
        if (b.t === "h") return <h4 key={i} className="reference-sheet-h" data-level={b.n}>{tidyNumbers(b.text)}</h4>;
        if (b.t === "p") return <p key={i} className="reference-sheet-p"><Val text={b.text} /></p>;
        if (b.t === "tree") return <Tree key={i} rows={b.rows} nm={nm} />;
        if (b.t === "kv") {
          const rows = b.rows.map((r) => ({ label: r.label, value: <span className="reference-sheet-v">{named(tidyNumbers(r.value), nm, <Val text={r.value} why />)}</span> }));
          const shown = rows.filter((_, n) => !BOOKKEEPING.has(b.rows[n]!.label));
          return shown.length > 0 ? <FindingDetail key={i} rows={shown} /> : null;
        }
        const keep = b.head.map((_, j) => b.rows.some((r) => !EMPTY.has(r[j] ?? "")) || b.rows.length === 0);
        return (
          <div key={i} className="reference-table-wrap" tabIndex={0} role="region" aria-label="Table">
            <table className="reference-sheet-table">
              <thead><tr>{b.head.map((c, j) => keep[j] && <th key={j}>{humanHeader(c)}</th>)}</tr></thead>
              <tbody>{b.rows.map((r, j) => <tr key={j}>{r.map((c, k) => keep[k] && <td key={k} title={tidyNumbers(c)}>{named(tidyNumbers(c), nm, <Val text={c} />)}</td>)}</tr>)}</tbody>
            </table>
          </div>
        );
      })}
    </div>
  );
}

const middle = (s: string, max = 44) => (s.length <= max ? s : `${s.slice(0, 20)}…${s.slice(-(max - 21))}`);

/** Reference shows static material plus the explicitly selected served-plan evidence. */
export function ReferenceFace({ payload, selectedPlanFinding: f = null, names, onJump }: ReferenceFaceProps) {
  const [q, setQ] = useState("");
  const nm = { names, onJump };
  // The plan finding's quantity, numbers and fix open its Declare row; source and witness its review.
  const to = (label: string, node: ReactNode, jump: Jump) => linkTo(onJump, label, node, jump);
  const at = f ? `Go to ${f.quantity}` : "";
  const own = { path: f?.quantity }, review = { path: f?.quantity, review: true };
  const devices = payload.devices.filter((d) => d.name.toLowerCase().includes(q.trim().toLowerCase()));
  return (
    <section className="reference-face" aria-label="Docs">
      {f !== null && (
        <section className="reference-plan-finding" aria-label="Selected plan finding">
          <FindingDetail
            rows={[
              { label: "Quantity", value: to(at, <code>{f.quantity}</code>, own) },
              { label: "Severity", value: <span className="reference-plan-finding-severity" data-severity={f.severity}>{f.severity}</span> },
              { label: "Observed", value: to(at, <span className="reference-plan-finding-observed">{f.observed}</span>, own) },
              { label: "Expected", value: to(at, <span className="reference-plan-finding-expected">{f.expected}</span>, own) },
              { label: "Fix", value: to(at, <span className="reference-plan-finding-action">{f.action}</span>, own) },
              { label: "Source", value: to(`Go to the review of ${f.quantity}`, f.source ?? "source unknown", review) },
              { label: "Witness", value: to(`Go to the review of ${f.quantity}`, f.witness ?? "witness unknown", review) },
              { label: "Locked", value: <span className="reference-plan-finding-locked-at">{f.locked_at ?? "date unknown"}</span> },
              ...((f.alternatives ?? []).length ? [{
                label: "Instead",
                value: <ul>{(f.alternatives ?? []).map((a, i) => <li key={i} data-kind={a.kind}>{to(`Show ${a.text}`, <>{a.text} ({a.kind})</>, insteadJump(a))}</li>)}</ul>,
              }] : []),
            ]}
          />
        </section>
      )}
      <div className="reference-face-drawers">
        {payload.drawers.map((drawer, index) => (
          <div key={`${drawer.title}-${index}`} className="reference-drawer">
            {/* provenance is a tooltip: the sentence names where the sheet came from, not what it says */}
            <Disclosure open={index === 0} summary={<span title={drawer.provenance}>{drawer.title}</span>}>
              {index > 0 && <div className="reference-drawer-cli"><CommandLine command={drawer.cli_line} display={middle(drawer.cli_line)} /></div>}
              <div className="reference-drawer-content"><Sheet text={drawer.content} {...nm} /></div>
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
                  <Disclosure summary={device.name} onOpen={onJump && (() => onJump({ name: device.id }))}><div className="reference-device-text"><Sheet text={device.text} {...nm} /></div></Disclosure>
                </div>
              ))}
            </div>
          </Disclosure>
        </div>
      )}
    </section>
  );
}
