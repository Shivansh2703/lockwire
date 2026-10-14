import { useState, type ReactNode } from "react";

import type { Finding, FindingLabel, RobotJoint, Target } from "../shared/api.gen";
import { Button, Disclosure, FindingDetail, FindingRow, StatusPill, type Status } from "./kit";
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
}

export type Chip = { label: string; title?: string };

/** The engine's short label for each target, its full id as the tooltip. */
export const targetChips = (targets: Target[]): Chip[] => targets.map((t) => ({ label: t.label, title: t.id }));

/** A finding can name dozens of parts; the row shows three and the count of the rest (whose
 * names are the "+N" chip's tooltip). */
/** A flagged finding with no part to blame says so, in the same chip slot. */
export const WHOLE: Chip[] = [{ label: "Whole robot" }];

export const capChips = (chips: Chip[]): Chip[] =>
  chips.length > 3
    ? [...chips.slice(0, 3), { label: `+${chips.length - 3}`, title: chips.slice(3).map((c) => c.title ?? c.label).join(", ") }]
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

export function FindingList({ items, selected, onSelect }: { items: FindingItem[]; selected: number | null; onSelect: (index: number | null) => void }) {
  const row = (it: FindingItem, pos: number) => {
    const i = it.index ?? pos;
    return (
      <div key={it.key} className={it.className} data-shape={it.shape}>
        <FindingRow severity={it.severity} title={it.title} chips={it.chips} note={it.note} selected={selected === i} onSelect={() => onSelect(selected === i ? null : i)} />
        {selected === i && it.detail}
      </div>
    );
  };
  // "Not checked" rows are many and quiet: one disclosure, not a wall of grey. A selected one opens it.
  const quiet = items.map((it, pos) => [it, pos] as const).filter(([it]) => it.severity === "muted");
  return (
    <div className="check-list">
      {items.map((it, pos) => (it.severity === "muted" ? null : row(it, pos)))}
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
export function findingItems(findings: Finding[], joints: RobotJoint[], onCitation?: (f: Finding) => void, labels: FindingLabel[] = []): FindingItem[] {
  const rank = (f: Finding): Status => (f.severity === "violation" ? "bad" : f.severity === "warning" ? "warn" : "muted");
  const groups: { key: string; members: number[] }[] = [];
  findings.forEach((f, i) => {
    const key = labels[i] ? `${f.severity}|${labels[i].label}` : `${i}`;
    const hit = groups.find((g) => g.key === key);
    if (hit) hit.members.push(i);
    else groups.push({ key, members: [i] });
  });
  const detail = (f: Finding, i: number) => (
    <div className="check-detail" key={i}>
      <FindingDetail
        rows={[
          { label: "Observed", value: f.observed },
          { label: "Expected", value: f.expected },
          { label: "Fix", value: labels[i]?.fix || f.action },
          ...(labels[i]?.instead ?? (f.alternatives ?? []).map((a) => ({ text: a.text, kind: a.kind }))).map((a, j) => ({
            label: j === 0 ? "Instead" : "",
            value: <span data-kind={a.kind}>{a.text} <em>{a.kind}</em></span>,
          })),
        ]}
      />
      <Disclosure summary="Show the exact finding">
        <FindingDetail rows={[
          { label: "Path", value: <code className="finding-path">{f.quantity}</code> },
          { label: "Source", value: f.source ?? "source unknown" },
          { label: "Witness", value: f.witness ?? "witness unknown" },
        ]} />
      </Disclosure>
      {onCitation && <Button variant="quiet" onClick={() => onCitation(f)}>View citation</Button>}
    </div>
  );
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
      chips: capChips(chips),
      className: "plan-finding",
      index: first,
      detail: members.length === 1 ? detail(f, first) : <>{members.map((i) => detail(findings[i]!, i))}</>,
    };
  });
}
