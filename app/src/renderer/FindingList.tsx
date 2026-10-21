import { useEffect, useRef, useState, type ReactNode } from "react";

import { fmtQty, tidyNumbers } from "./kit/format";
import type { Finding, FindingLabel, RobotJoint, Target } from "../shared/api.gen";
import { Button, Disclosure, FindingDetail, FindingRow, Jumper, StatusPill, type KitChip, type Status } from "./kit";
import { mergeFix } from "./mergeFix";
import { insteadJump, type Jump, type OnJump } from "./jump";
import { jointsOfPath } from "./scene/robotScene";

// The row list Check, Plan and Test share: one FindingRow per item, the selected
// one open inline. Callers say what a row and its detail are; this owns the markup.
export const VERDICT: Record<string, [Status, string]> = {
  GO: ["go", "GO"], GO_WITH_NOTES: ["warn", "GO with notes"], "NO-GO": ["bad", "NO-GO"], REFUSED: ["bad", "REFUSED"], NOT_SCORED: ["muted", "Not scored"],
};

export const verdictPill = (verdict: string) => {
  const [status, label] = VERDICT[verdict] ?? ["muted", verdict];
  return <StatusPill status={status}>{label}</StatusPill>;
};

export interface FindingItem {
  key: string;
  severity: Status;
  title: ReactNode;
  chips?: Chip[];
  note?: ReactNode;
  className?: string;
  shape?: string;
  detail?: ReactNode;
  /** Position in the engine's list, when the rows were reordered; selection speaks in these. */
  index?: number;
  /** Chips act (select their part) when given. */
  onChip?: (chip: Chip) => void;
}

/** A chip remembers the engine target it came from, so a click can light that target's parts. */
export type Chip = KitChip & { target?: Target; whole?: boolean };

/** The engine's short label for each target, its full id as the tooltip. */
export const targetChips = (targets: Target[]): Chip[] => targets.map((t) => ({ label: t.label, title: t.id, target: t, rail: t.kind === "rail" }));

/** What a chip click asks for: its target's parts, a bare joint name, or the whole robot. */
export const chipJump = (c: Chip): Jump => (c.whole ? { whole: true } : c.target ? { targets: [c.target] } : { parts: [c.label] });

/** A flagged finding with no part to blame says so, in the same chip slot. */
export const WHOLE: Chip[] = [{ label: "Whole robot", whole: true }];

/** Two chips and the count of the rest; a long name is cut at its end (the full name is the tooltip). */
export const capChips = (chips: Chip[]): Chip[] =>
  chips.length > 2
    ? [...chips.slice(0, 2), { label: `+${chips.length - 2}`, title: chips.slice(2).map((c) => c.title ?? c.label).join(", "), more: chips.slice(2) }]
    : chips;

/** The 3D view's text twin ("3 parts flagged: Camera, Jetson, Radio"), from the engine's own
 * target labels, for an aria-live region: the robot is not the only place the answer lives. */
export function flagSummary(flagged: string[], targets: Target[]): string {
  const names = new Map(targets.map((t) => [t.id, t.label]));
  const labels = [...new Set(flagged.map((id) => names.get(id)).filter((l): l is string => Boolean(l)))];
  if (labels.length === 0) return "";
  const more = labels.length > 3 ? ` and ${labels.length - 3} more` : "";
  return `${labels.length} ${labels.length === 1 ? "part" : "parts"} flagged: ${labels.slice(0, 3).join(", ")}${more}`;
}

/** Selection owned by the parent (so the robot can light up) or, absent that, by the caller. */
export function useSelection(selected?: number | null, onSelect?: (index: number | null) => void): [number | null, (index: number | null) => void] {
  const [own, setOwn] = useState<number | null>(null);
  return selected === undefined || !onSelect ? [own, setOwn] : [selected, onSelect];
}

export function FindingList({ items, selected, onSelect, between }: { items: FindingItem[]; selected: number | null; onSelect: (index: number | null) => void; /** Rows that sit after the problems and before the "not checked" group (Test: what agreed). */ between?: ReactNode }) {
  const list = useRef<HTMLDivElement>(null);
  // A selection made elsewhere (a chip, another tab) scrolls its row into view.
  useEffect(() => {
    list.current?.querySelector(".kit-finding.is-selected")?.scrollIntoView?.({ block: "nearest" });
  }, [selected]);
  const row = (it: FindingItem, pos: number) => {
    const i = it.index ?? pos;
    const onChip = it.onChip;
    return (
      <div key={it.key} className={it.className} data-shape={it.shape}>
        <FindingRow severity={it.severity} title={it.title} chips={it.chips} note={it.note} selected={selected === i} onSelect={() => onSelect(selected === i ? null : i)} onChip={onChip && ((c) => onChip(c as Chip))} />
        {selected === i && it.detail}
      </div>
    );
  };
  // "Not checked" rows are many and quiet: one disclosure, not a wall of grey. A selected one opens it.
  const quiet = items.map((it, pos) => [it, pos] as const).filter(([it]) => it.severity === "muted");
  return (
    <div className="check-list" ref={list}>
      {items.map((it, pos) => (it.severity === "muted" ? null : row(it, pos)))}
      {between}
      {quiet.length > 0 && (
        <Disclosure summary={`${quiet.length} not checked`} open={quiet.some(([it, pos]) => (it.index ?? pos) === selected) || undefined}>
          {quiet.map(([it, pos]) => row(it, pos))}
        </Disclosure>
      )}
    </div>
  );
}

/** Plan / Test findings (`Finding`, not `Card`) as rows: the engine's plain label names the row, the
 * parts it points at are its chips, and the open row shows the evidence, with provenance (path,
 * source, witness) behind a disclosure. Repeats of one check with one verdict are one row. */
export function findingItems(findings: Finding[], joints: RobotJoint[], onCitation?: (f: Finding) => void, labels: FindingLabel[] = [], onJump?: OnJump, rawExact = false): FindingItem[] {
  const rank = (f: Finding): Status => (f.severity === "violation" ? "bad" : f.severity === "warning" ? "warn" : "muted");
  const groups: { key: string; members: number[] }[] = [];
  findings.forEach((f, i) => {
    const key = labels[i] ? `${f.severity}|${labels[i].label}` : `${i}`;
    const hit = groups.find((g) => g.key === key);
    if (hit) hit.members.push(i);
    else groups.push({ key, members: [i] });
  });
  const detail = (f: Finding, i: number) => <PlanDetail key={i} f={f} label={labels[i]} onJump={onJump} onCitation={onCitation} />;
  const order = { bad: 0, warn: 1, muted: 2, go: 3 };
  return groups.sort((a, b) => order[rank(findings[a.members[0]!]!)] - order[rank(findings[b.members[0]!]!)]).map(({ key, members }) => {
    const first = members[0]!;
    const f = findings[first]!;
    const chips = members.flatMap((i) => labels[i] ? targetChips(labels[i].targets) : jointsOfPath(findings[i]!.quantity, joints).map((label) => ({ label })))
      .filter((c, n, all) => all.findIndex((d) => d.label === c.label) === n);
    if (labels[first]?.whole && rank(f) !== "muted") chips.push(...WHOLE);
    const label = labels[first]?.label ?? f.quantity;
    return {
      key: `${f.quantity}/${key}`,
      severity: rank(f),
      title: members.length > 1 && chips.length === 0 ? `${label} x${members.length}` : label,
      chips: liveChips(capChips(chips), onJump),
      onChip: onJump && ((c: Chip) => onJump(chipJump(c))),
      className: "plan-finding",
      index: first,
      detail: (() => {
        const heads = modeHeads(members.map((i) => findings[i]!), members.map((i) => labels[i]?.targets.find((t) => t.kind === "rail")?.label));
        if (!heads) return <>{members.map((i) => detail(findings[i]!, i))}</>;
        // Parts on one overloaded rail share a header: one block, one Fix naming them all, one exact finding.
        const blocks = [...new Set(heads)].map((h) => {
          const at = members.filter((_, n) => heads[n] === h);
          const fixes = at.map((i) => labels[i]?.fix || findings[i]!.action);
          const merged = new Set(fixes).size > 1 ? mergeFix(fixes) : null;
          const keep = at.filter((_, n) => merged ? n === 0 : fixes.indexOf(fixes[n]!) === n); // else one per distinct text
          return keep.map((i) => ({ h, i, label: merged && labels[i] ? { ...labels[i]!, fix: merged } : labels[i] }));
        }).flat();
        return <ModeBlocks heads={blocks.map((b) => b.h)} details={blocks.map((b) => <PlanDetail key={b.i} f={findings[b.i]!} label={b.label} onJump={onJump} onCitation={onCitation} noEvidence rawExact={rawExact} />)} />;
      })(),
    };
  });
}

/** A named thing as a link when its jump lands somewhere; else, when given, a link that opens the
 * exact finding; else plain text (no button that does nothing). */
export function linkTo(onJump: OnJump | undefined, label: string, node: ReactNode, jump: Jump, exact?: () => void): ReactNode {
  if (onJump?.can(jump)) return <Jumper label={label} onClick={() => onJump(jump)}>{node}</Jumper>;
  return exact ? <Jumper label="Show the exact finding" onClick={exact}>{node}</Jumper> : node;
}

/** Chips that land nowhere (an id the robot does not draw) stay plain. */
export const liveChips = (chips: Chip[], onJump?: OnJump): Chip[] =>
  chips.map((c) => (c.more ? { ...c, more: liveChips(c.more as Chip[], onJump) } : { ...c, dead: !onJump?.can(chipJump(c)) }));

/** One Plan / Test finding opened: its evidence, each named thing leading on, provenance behind a
 * disclosure that a link with nowhere else to go opens instead. */
const MASS = new Set(["kg", "g"]);
const MODE = /\(draw mode: ([^)]+)\)|at mode '([^']+)'/;
const modeOf = (observed: string) => { const m = MODE.exec(observed); return m ? m[1] ?? m[2] : /rolled-up draw/.test(observed) ? "Load" : undefined; };
const num = (q: { value: number; unit: string }, vs?: number) => fmtQty(q.value, q.unit, vs);
/** "<= 15.60 W" in plain words; anything longer (a provenance tail) stays as the engine wrote it. */
const maxOf = (v: string) => (/^<= \S+ \S+$/.test(v) ? `max ${v.slice(3)}` : v);
const plainObserved = (v: string) => { const m = /^(\S+ \S+) \(draw mode: ([^)]+)\)$/.exec(v); return m ? `${m[1]} · ${m[2]}` : v; };

/** One header per draw mode ("Stall · 194.4 W of 60 W"): numbers from the typed quantities, only the
 * mode name from the text; the rail's name joins when two headers would read the same; null when the
 * findings are not modes of one check. */
export function modeHeads(fs: Pick<Finding, "observed" | "observed_quantity" | "expected_quantity">[], rails: (string | undefined)[]): string[] | null {
  const heads = fs.map((f) => {
    const mode = modeOf(f.observed ?? "") ?? (MASS.has(f.observed_quantity?.unit ?? "") ? "mass" : undefined);
    if (!mode || !f.observed_quantity || !f.expected_quantity) return null;
    return [mode[0]!.toUpperCase() + mode.slice(1), `${num(f.observed_quantity, f.expected_quantity.value)} of ${num(f.expected_quantity, f.observed_quantity.value)}`] as const;
  });
  if (!heads.every(Boolean)) return null;
  const hs = heads as (readonly [string, string])[];
  return hs.map(([m, n], i) => (hs.some(([m2, n2], j) => j !== i && m2 === m && n2 !== n) && rails[i] ? `${m} · ${rails[i]} · ${n}` : `${m} · ${n}`));
}

/** Every mode under its own header and caret; the first one open. */
export function ModeBlocks({ heads, details }: { heads: string[]; details: ReactNode[] }) {
  const uniq = [...new Set(heads)]; // identical headers (two devices, one overload) share one block
  return <>{uniq.map((h, n) => <Disclosure key={h} summary={h} open={n === 0 || undefined}>{details.filter((_, i) => heads[i] === h)}</Disclosure>)}</>;
}

function PlanDetail({ f, label, onJump, onCitation, noEvidence, rawExact }: { f: Finding; label?: FindingLabel; onJump?: OnJump; onCitation?: (f: Finding) => void; noEvidence?: boolean; rawExact?: boolean }) {
  const [exact, setExact] = useState(false);
  const open = exact ? undefined : () => setExact(true);
  const own: Jump = { path: f.quantity, targets: label?.targets };
  const at = `Go to ${f.quantity}`;
  return (
    <div className="check-detail">
      <FindingDetail
        rows={[
          ...(noEvidence ? [] : [
            { label: "Observed", value: linkTo(onJump, at, plainObserved(tidyNumbers(f.observed ?? "", f.expected_quantity?.value)), own, open) },
            { label: "Expected", value: linkTo(onJump, at, maxOf(tidyNumbers(f.expected ?? "", f.observed_quantity?.value)), own, open) },
          ]),
          { label: "Fix", value: linkTo(onJump, at, label?.fix || f.action, own, open) },
          ...(label?.instead ?? f.alternatives ?? []).map((a, j) => ({
            label: j === 0 ? "Instead" : "",
            value: linkTo(onJump, `Show ${a.text}`, <span data-kind={a.kind}><small className="kit-kind kit-kind-lead">{a.kind}</small>{a.text}</span>, insteadJump(a)),
          })),
        ]}
      />
      <Disclosure summary="Show the exact finding" open={exact || undefined}>
        <FindingDetail rows={[
          ...(noEvidence && rawExact ? [
            { label: "Observed", value: f.observed ?? "" },
            { label: "Expected", value: f.expected ?? "" },
          ] : []),
          { label: "Path", value: linkTo(onJump, at, <code className="finding-path">{f.quantity}</code>, { path: f.quantity }) },
          { label: "Source", value: linkTo(onJump, `Go to the review of ${f.quantity}`, f.source ?? "source unknown", { path: f.quantity, review: true }) },
          { label: "Witness", value: linkTo(onJump, `Go to the review of ${f.quantity}`, f.witness ?? "witness unknown", { path: f.quantity, review: true }) },
        ]} />
        {onCitation && <Button variant="quiet" onClick={() => onCitation(f)}>View citation</Button>}
      </Disclosure>
    </div>
  );
}
