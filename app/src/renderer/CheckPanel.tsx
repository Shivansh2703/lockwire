import { useState, type ReactNode } from "react";

import type { Figure as ApiFigure } from "../shared/api.gen";
import type { Card, CheckFacePayload } from "./checkFacePayload";
import { FindingList, ModeBlocks, VERDICT, WHOLE, capChips, chipJump, linkTo, liveChips, modeHeads, targetChips, type Chip, type FindingItem } from "./FindingList";
import { insteadJump, type Jump, type OnJump } from "./jump";
import { Button, CommandLine, Disclosure, Figure, FindingDetail, FindingRow, Jumper, Panel, StatusPill } from "./kit";

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
  payload, selected, onSelect, onRecheck, pills, more, onJump,
}: {
  payload: CheckFacePayload;
  selected: number | null;
  onSelect: (index: number | null) => void;
  onRecheck?: () => void;
  /** Extra header pills from the optional pro/ slot. */
  pills?: ReactNode;
  /** Extra section under the findings from the optional pro/ slot. */
  more?: ReactNode;
  /** Chips, numbers, fixes and alternatives lead to the part or the Declare row they name. */
  onJump?: OnJump;
}) {
  const [verdictStatus, verdictLabel] = VERDICT[payload.verdict] ?? ["muted", payload.verdict];
  const owners = [...new Set(payload.findings.map((f) => f.owner).filter((o): o is string => Boolean(o)))];
  // "Frames without mass" is normal on a fresh robot: a quiet note under the rows, not a problem.
  const isNote = (i: number) => payload.cards[i]!.quantity === MASSLESS;
  const noteCard = payload.cards.find((_, i) => isNote(i));
  const groups = groupCards(payload.cards).filter((g) => !isNote(g.members[0]!));
  const problem = (g: Group) => ["violation", "warning"].includes(payload.cards[g.members[0]!]!.severity);
  const firstProblem = groups.find(problem)?.members[0];
  const firstQuiet = groups.find((g) => !problem(g))?.members[0];
  const { problems, quiet } = countWords(groups, payload.cards);
  return (
    <Panel
      title="Findings"
      actions={
        <>
          <span className={`check-headline check-headline-left verdict-${payload.verdict.toLowerCase().replace(/_/g, "-")}`} role="status" title={payload.headline}>
            {firstProblem === undefined || selected === firstProblem ? <StatusPill status={verdictStatus}>{verdictLabel}</StatusPill> : (
              <button type="button" className="check-headline-btn" aria-label={`${verdictLabel}: show the first problem`} onClick={() => onSelect(firstProblem)}>
                <StatusPill status={verdictStatus}>{verdictLabel}</StatusPill>
                <span className="check-count">{problems}</span>
              </button>
            )}
            {(firstProblem === undefined || selected === firstProblem) && <span className="check-count">{problems}</span>}
            {quiet && firstQuiet !== undefined && <span className="check-count">{" · "}<Jumper label={`Show the ${quiet}`} onClick={() => onSelect(firstQuiet)}>{quiet}</Jumper></span>}
          </span>
          {onRecheck && <Button variant="primary" onClick={onRecheck}>Re-check</Button>}
        </>
      }
    >
      {(pills || owners.length > 1) && (
        <div className="check-pills">
          {pills}
          {owners.map((o) => {
            // An owner pill selects that owner's first card (stays in Check: who owns it, then what it is).
            const card = payload.cards.findIndex((c) => c.owner === o);
            const pill = <><span className="kit-sr">Owner: </span><StatusPill status="muted">{ownerWord(o)}</StatusPill></>;
            return card < 0
              ? <span key={o} title="Who owns these findings">{pill}</span>
              : <button key={o} type="button" className="kit-pill-btn" title={`Show ${ownerWord(o)}'s finding`} aria-pressed={selected === card} onClick={() => onSelect(selected === card ? null : card)}>{pill}</button>;
          })}
        </div>
      )}
      <FindingList selected={groupOf(groups, selected)} onSelect={(g) => onSelect(g === null ? null : groups[g]!.members[0]!)} items={groups.map((g) => groupItem(payload.cards, g, selected, onSelect, onJump, payload.cli_line))} />
      {noteCard && <p className="check-note">{noteWords(noteCard)}</p>}
      {more}
    </Panel>
  );
}

/** The mode header ("Stall · 194.4 W of 60 W") from the card's typed figures; null without them. */
const headOf = (card: Card) => modeHeads([{ observed: card.raw, observed_quantity: card.observed, expected_quantity: card.expected }], [card.targets.find((t) => t.kind === "rail")?.label])?.[0];

/** One card opened: with typed numbers, the mode header, one Fix line, and the figures behind
 * "Show the exact finding"; without, the figures lead as before. */
function Detail({ card, onJump, cli, bare }: { card: Card; onJump?: OnJump; cli?: string | null; bare?: boolean }) {
  // The "i" opens the exact finding, where the note is written out.
  const [exact, setExact] = useState(false);
  const info = card.figure_note ? <button type="button" className="finding-info" aria-label={card.figure_note} title={card.figure_note} onClick={() => setExact(true)}>i</button> : null;
  // A number or the fix leads to the quantity's Declare row (else the card's parts light).
  const own: Jump = { path: card.quantity, targets: card.targets };
  // Nowhere to go: the link opens the exact finding (while it is shut), else the text stays plain.
  const to = (label: string, node: ReactNode, jump: Jump = own) => linkTo(onJump, label, node, jump, exact ? undefined : () => setExact(true));
  const at = card.quantity ? `Go to ${card.quantity}` : "Show its parts";
  const head = headOf(card);
  const figures: { label: string; value: ReactNode }[] = [
    card.observed && { label: "Observed", value: <>{to(at, fig(card.observed))}{info}</> },
    card.expected && { label: "Expected", value: <>{to(at, fig(card.expected))}{card.observed ? null : info}</> },
  ].flatMap((r) => (r ? [r] : []));
  const instead = [
    ...card.instead.map((a, i) => ({ label: i === 0 ? "Instead" : "", value: to(`Show ${a.text}`, <span className="finding-alt" title={a.text}>{a.text}</span>, insteadJump(a)) })),
  ];
  const fix = { label: "Fix", value: to(at, <span className="finding-fix">{card.fix}</span>) };
  const body = (
    <div className="check-detail">
      <FindingDetail rows={head ? [fix, ...instead] : [...figures, ...instead, fix]} />
      <Disclosure summary="Show the exact finding" open={exact || undefined}>
        {head && <FindingDetail rows={figures} />}
        {!card.observed && !card.expected && card.body && <p className="finding-body">{card.body}</p>}
        {card.figure_note && <p className="finding-note">{card.figure_note}</p>}
        <pre className="finding-raw">{card.raw}</pre>
        {cli && <CommandLine command={cli} />}
      </Disclosure>
    </div>
  );
  return head && !bare ? <ModeBlocks heads={[head]} details={[body]} /> : body;
}

const MASSLESS = "model/links/massless_frames";
/** A count, never the link ids: the engine's raw line names them ("observed  2 massless frames (...): a, b"). */
const noteWords = (c: Card) => {
  const n = /observed\s+(\d+) massless/.exec(c.raw)?.[1];
  return `${n ?? "Some"} ${n === "1" ? "frame" : "frames"} without mass — normal for sensor and mount frames`;
};

// Repeated findings (same check, same wording) are one row: "Drives joint x12". Grouping keys are
// the engine's own (shape, severity, title); `members` are indices into `cards` so selection,
// and the robot's highlight, still speak in card indices.
interface Group { members: number[] }

/** "Supply 5 V rail overloaded in the 7W mode" and "... in the stall mode" are one rail's one fault. */
const noMode = (title: string) => title.replace(/( rail (?:overloaded|load)) in the .+ mode$/, "$1");

const groupCards = (cards: Card[]): Group[] => {
  const byKey = new Map<string, Group>();
  const out: Group[] = [];
  cards.forEach((c, i) => {
    const key = `${c.severity}|${c.shape}|${c.group_title || noMode(c.title)}`;
    const hit = byKey.get(key);
    if (hit) hit.members.push(i);
    else { const g = { members: [i] }; byKey.set(key, g); out.push(g); }
  });
  return out;
};

const countWords = (groups: Group[], cards: Card[]) => {
  const quiet = groups.filter((g) => cards[g.members[0]!]!.severity !== "violation" && cards[g.members[0]!]!.severity !== "warning").length;
  // A rail's modes are one row but still its engine findings: say both ("1 problem · 2 modes").
  const bad = groups.filter((g) => !(cards[g.members[0]!]!.severity !== "violation" && cards[g.members[0]!]!.severity !== "warning"));
  const p = bad.length;
  const engine = bad.reduce((n, g) => n + (g.members.some((i) => noMode(cards[i]!.title) !== cards[i]!.title) ? g.members.length : 1), 0);
  return { problems: `${p} ${p === 1 ? "problem" : "problems"}${engine > p ? ` · ${engine} modes` : ""}`, quiet: quiet ? `${quiet} not checked` : "" };
};

const groupOf = (groups: Group[], selected: number | null) =>
  selected === null ? null : groups.findIndex((g) => g.members.includes(selected));

function groupItem(cards: Card[], g: Group, selected: number | null, onSelect: (index: number | null) => void, onJump?: OnJump, cli?: string | null): FindingItem {
  const first = cards[g.members[0]!]!;
  const onChip = onJump && ((c: Chip) => onJump(chipJump(c)));
  const severity = first.severity === "violation" ? "bad" : first.severity === "warning" ? "warn" : "muted";
  const className = `finding-card severity-${first.severity}`;
  if (g.members.length === 1) {
    return { key: `${first.shape}-${g.members[0]!}`, severity, title: headOf(first) ? noMode(first.title) : first.title, chips: liveChips(first.whole && severity !== "muted" ? WHOLE : capChips(targetChips(first.targets)), onJump), onChip, className, shape: first.shape, detail: <Detail card={first} onJump={onJump} cli={cli} /> };
  }
  const heads = modeHeads(g.members.map((i) => ({ observed: cards[i]!.raw, observed_quantity: cards[i]!.observed, expected_quantity: cards[i]!.expected })), g.members.map((i) => cards[i]!.targets.find((t) => t.kind === "rail")?.label));
  const targets = g.members.flatMap((i) => cards[i]!.targets).filter((t, n, all) => all.findIndex((u) => u.id === t.id && u.kind === t.kind) === n);
  const merged = !first.group_title && heads && g.members.some((i) => noMode(cards[i]!.title) !== cards[i]!.title);
  if (merged) {
    return { key: `${first.shape}-${g.members[0]!}`, severity, title: noMode(first.title), chips: liveChips(capChips(targetChips(targets)), onJump), onChip, className, shape: first.shape, detail: <ModeBlocks heads={heads} details={g.members.map((i) => <Detail key={i} card={cards[i]!} onJump={onJump} cli={cli} bare />)} /> };
  }
  return {
    key: `${first.shape}-${g.members[0]!}`,
    severity,
    title: first.group_title ? first.group_title.replace("{n}", String(g.members.length)) : `${first.label} x${g.members.length}`,
    chips: liveChips(capChips(targetChips(targets)), onJump),
    onChip,
    className,
    shape: first.shape,
    detail: (
      <div className="check-group">
        <p className="check-group-note">{first.title}</p>
        {g.members.map((i) => (
          <div key={i}>
            <FindingRow severity={severity} title={cards[i]!.targets[0]?.label ?? cards[i]!.label} chips={liveChips(targetChips(cards[i]!.targets.slice(1)), onJump)} selected={selected === i} onSelect={() => onSelect(selected === i ? g.members[0]! : i)} onChip={onChip && ((c) => onChip(c as Chip))} />
            {selected === i && <Detail card={cards[i]!} onJump={onJump} cli={cli} />}
          </div>
        ))}
      </div>
    ),
  };
}
