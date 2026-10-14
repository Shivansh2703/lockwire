import type { ReactNode } from "react";

import type { Figure as ApiFigure } from "../shared/api.gen";
import type { Card, CheckFacePayload } from "./checkFacePayload";
import { FindingList, VERDICT, WHOLE, capChips, targetChips, type FindingItem } from "./FindingList";
import { Button, Disclosure, Figure, FindingDetail, FindingRow, Panel, StatusPill } from "./kit";

// Check: the verdict, one row per finding, the selected one open inline. Every
// word is the payload's own; the only fixed words are the panel title, the verdict label,
// the count caption and the row captions.
const fig = (f: ApiFigure) => (
  <span title={f.by}>
    <Figure value={f.value} unit={f.unit || undefined} kind={f.kind} />
  </span>
);

// Owner ids are engine slugs; show them as words ("power-integration" -> "Power").
const ownerWord = (o: string) => { const w = o.replace(/-integration$/, "").replace(/[-_]/g, " "); return w.replace(/^./, (c) => c.toUpperCase()); };

export function CheckPanel({
  payload, selected, onSelect, onRecheck, pills, more,
}: {
  payload: CheckFacePayload;
  selected: number | null;
  onSelect: (index: number | null) => void;
  onRecheck?: () => void;
  /** Extra header pills from the optional pro/ slot. */
  pills?: ReactNode;
  /** Extra section under the findings from the optional pro/ slot. */
  more?: ReactNode;
}) {
  const [verdictStatus, verdictLabel] = VERDICT[payload.verdict] ?? ["muted", payload.verdict];
  const owners = [...new Set(payload.findings.map((f) => f.owner).filter((o): o is string => Boolean(o)))];
  const groups = groupCards(payload.cards);
  return (
    <Panel
      title="Findings"
      actions={
        <>
          <span className={`check-headline check-headline-left verdict-${payload.verdict.toLowerCase().replace(/_/g, "-")}`} role="status" title={payload.headline}>
            <StatusPill status={verdictStatus}>{verdictLabel}</StatusPill>
            <span className="check-count">{countWord(groups, payload.cards)}</span>
          </span>
          {onRecheck && <Button variant="primary" onClick={onRecheck}>Re-check</Button>}
        </>
      }
    >
      {(pills || owners.length > 0) && (
        <div className="check-pills">
          {pills}
          {owners.map((o) => <span key={o} title="Who owns these findings"><span className="kit-sr">Owner: </span><StatusPill status="muted">{ownerWord(o)}</StatusPill></span>)}
        </div>
      )}
      <FindingList selected={groupOf(groups, selected)} onSelect={(g) => onSelect(g === null ? null : groups[g]!.members[0]!)} items={groups.map((g) => groupItem(payload.cards, g, selected, onSelect))} />
      {more}
    </Panel>
  );
}

function Detail({ card }: { card: Card }) {
  // The note explains the figures, so its "i" sits inline beside the first figure.
  const info = card.figure_note ? <button type="button" className="finding-info" aria-label={card.figure_note} title={card.figure_note}>i</button> : null;
  const rows: { label: string; value: ReactNode }[] = [
    card.observed && { label: "Observed", value: <>{fig(card.observed)}{info}</> },
    card.expected && { label: "Expected", value: <>{fig(card.expected)}{card.observed ? null : info}</> },
    // No figures to show: the engine's plain sentence is the only place the reason is said in words.
    !card.observed && !card.expected && card.body && { label: "Found", value: <span className="finding-body">{card.body}</span> },
    ...card.instead.map((a, i) => ({ label: i === 0 ? "Instead" : "", value: <span>{a.text} <em>{a.kind}</em></span> })),
    { label: "Fix", value: <span className="finding-fix">{card.fix}</span> },
  ].flatMap((r) => (r ? [r] : []));
  return (
    <div className="check-detail">
      <FindingDetail rows={rows} />
      <Disclosure summary="Show the exact finding">
        {card.figure_note && <p className="finding-note">{card.figure_note}</p>}
        <pre className="finding-raw">{card.raw}</pre>
      </Disclosure>
    </div>
  );
}

// Repeated findings (same check, same wording) are one row: "Drives joint x12". Grouping keys are
// the engine's own (shape, severity, title); `members` are indices into `cards` so selection,
// and the robot's highlight, still speak in card indices.
interface Group { members: number[] }

const groupCards = (cards: Card[]): Group[] => {
  const byKey = new Map<string, Group>();
  const out: Group[] = [];
  cards.forEach((c, i) => {
    const key = `${c.severity}|${c.shape}|${c.group_title || c.title}`;
    const hit = byKey.get(key);
    if (hit) hit.members.push(i);
    else { const g = { members: [i] }; byKey.set(key, g); out.push(g); }
  });
  return out;
};

const countWord = (groups: Group[], cards: Card[]) => {
  const quiet = groups.filter((g) => cards[g.members[0]!]!.severity !== "violation" && cards[g.members[0]!]!.severity !== "warning").length;
  const p = groups.length - quiet;
  return `${p} ${p === 1 ? "problem" : "problems"}${quiet ? ` · ${quiet} not checked` : ""}`;
};

const groupOf = (groups: Group[], selected: number | null) =>
  selected === null ? null : groups.findIndex((g) => g.members.includes(selected));

function groupItem(cards: Card[], g: Group, selected: number | null, onSelect: (index: number | null) => void): FindingItem {
  const first = cards[g.members[0]!]!;
  const severity = first.severity === "violation" ? "bad" : first.severity === "warning" ? "warn" : "muted";
  const className = `finding-card severity-${first.severity}`;
  if (g.members.length === 1) {
    return { key: `${first.shape}-${g.members[0]!}`, severity, title: first.title, chips: first.whole && severity !== "muted" ? WHOLE : capChips(targetChips(first.targets)), className, shape: first.shape, detail: <Detail card={first} /> };
  }
  const targets = g.members.flatMap((i) => cards[i]!.targets).filter((t, n, all) => all.findIndex((u) => u.id === t.id && u.kind === t.kind) === n);
  return {
    key: `${first.shape}-${g.members[0]!}`,
    severity,
    title: first.group_title ? first.group_title.replace("{n}", String(g.members.length)) : `${first.label} x${g.members.length}`,
    chips: capChips(targetChips(targets)),
    className,
    shape: first.shape,
    detail: (
      <div className="check-group">
        <p className="check-group-note">{first.title}</p>
        {g.members.map((i) => (
          <div key={i}>
            <FindingRow severity={severity} title={cards[i]!.targets[0]?.label ?? cards[i]!.label} chips={targetChips(cards[i]!.targets.slice(1))} selected={selected === i} onSelect={() => onSelect(selected === i ? g.members[0]! : i)} />
            {selected === i && <Detail card={cards[i]!} />}
          </div>
        ))}
      </div>
    ),
  };
}
