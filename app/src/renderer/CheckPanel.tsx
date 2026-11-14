import { Fragment, useEffect, useState, type ReactNode } from "react";

import type { Figure as ApiFigure, RequirementRow, RobotGraph } from "../shared/api.gen";
import type { Card, CheckFacePayload } from "./checkFacePayload";
import { FindingList, ModeBlocks, VERDICT, WHOLE, capChips, chipJump, linkTo, liveChips, modeHeads, targetChips, type Chip, type FindingItem } from "./FindingList";
import { insteadJump, type Jump, type OnJump } from "./jump";
import { graphLabels, partNames } from "./partNames";
import { Button, CommandLine, Disclosure, Figure, FindingDetail, FindingRow, Panel, StatusPill } from "./kit";
import { MASS_NOMINAL, massLine } from "./MeasureCompareDeclare";
import { SwapPanel } from "./SwapPanel";

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

// GO whose only scored item is the lock agreeing with itself: the page payload's adds_up is empty
// (nothing physical compared) while the verdict stays GO. Say so beside the word.
const SELF_ONLY = "Locked. Nothing physical compared yet. Declare the robot's mass or a part's datasheet figure to compare it.";
export const selfAgreementOnly = (p: Pick<CheckFacePayload, "verdict" | "adds_up">) => p.verdict === "GO" && p.adds_up?.length === 0;

export function CheckPanel({
  payload, graph, selected, onSelect, onRecheck, pills, more, onJump, onCompare, notice, saved, contract, expectedRevision, onSwapped,
}: {
  payload: CheckFacePayload;
  /** The robot graph: its nodes carry each part's display name (what GraphView shows). */
  graph?: RobotGraph | null;
  selected: number | null;
  onSelect: (index: number | null) => void;
  onRecheck?: () => void;
  /** Extra header pills from the optional pro/ slot. */
  pills?: ReactNode;
  /** Extra section under the findings from the optional pro/ slot. */
  more?: ReactNode;
  /** Chips, numbers, fixes and alternatives lead to the part or the Declare row they name. */
  onJump?: OnJump;
  /** "Compare with datasheet": the whole-robot button on the verdict card (part rows say "Declare it"). */
  onCompare?: () => void;
  /** One row under the verdict (the mesh-folder fixer): inside the panel, never over the verdict. */
  notice?: ReactNode;
  /** Where the lockfile was just saved ("saved beside your model"): the count line's last item. */
  saved?: string;
  /** The open lockfile and its revision: with `onSwapped`, a servo finding offers Part Swap. */
  contract?: string;
  expectedRevision?: string;
  /** A swap was written: re-check. */
  onSwapped?: (lines: string[]) => void;
}) {
  const [swapJoint, setSwapJoint] = useState<string | null>(null);
  // Display only: the verdict, its count and the selection still speak for every finding.
  const [sev, setSev] = useState("");
  const [part, setPart] = useState("");
  const [verdictStatus, verdictLabel] = VERDICT[payload.verdict] ?? ["muted", payload.verdict];
  const owners = [...new Set(payload.findings.map((f) => f.owner).filter((o): o is string => Boolean(o)))];
  // "Frames without mass" is normal on a fresh robot: a quiet note under the rows, not a problem.
  const isNote = (i: number) => payload.cards[i]!.quantity === MASSLESS;
  const noteCard = payload.cards.find((_, i) => isNote(i));
  const groups = groupCards(payload.cards).filter((g) => !isNote(g.members[0]!));
  const problem = (g: Group) => ["violation", "warning"].includes(payload.cards[g.members[0]!]!.severity);
  const firstProblem = groups.find(problem)?.members[0];
  const { problems, quiet } = countWords(groups, payload.cards);
  // The parts the engine says findings involve (Card.involves), named by a target's label when one matches.
  const nameOf = partNames([...payload.cards.flatMap((c) => c.targets.map((t) => [t.id, t.label] as const)), ...graphLabels(graph)]);
  const parts = [...new Set(groups.flatMap((g) => g.members.flatMap((i) => payload.cards[i]!.involves ?? [])))].map((id) => ({ id, name: nameOf(id) }));
  const keep = (g: Group) => (!sev || payload.cards[g.members[0]!]!.severity === sev) && (!part || g.members.some((i) => payload.cards[i]!.involves?.includes(part)));
  const shown = groups.filter(keep).length;
  const accepted = payload.accepted ?? [];
  const count = [...problems, quiet, accepted.length ? `${accepted.length} accepted` : "", saved].filter(Boolean)
    .flatMap((c, i) => [...(i ? [" · "] : []), <Fragment key={i}>{c}</Fragment>]);
  // Check lands on the first problem, already open: nothing to tap to see what is wrong (mockup 2B).
  useEffect(() => { if (selected === null && firstProblem !== undefined) onSelect(firstProblem); }, [payload.cards]);
  const onSwap = contract && expectedRevision && onSwapped ? setSwapJoint : undefined;
  if (swapJoint && contract && expectedRevision && onSwapped) {
    return <SwapPanel contract={contract} revision={expectedRevision} joint={swapJoint} onClose={() => setSwapJoint(null)} onApplied={(lines) => { setSwapJoint(null); onSwapped(lines); }} />;
  }
  return (
    <Panel>
      <div className={`check-verdict check-verdict-${verdictStatus}`}>
        <div className={`check-headline verdict-${payload.verdict.toLowerCase().replace(/_/g, "-")}`} role="status" title={payload.headline}>
          <span className="check-verdict-kicker">Verdict</span>
          <span className="check-verdict-word">{verdictLabel}</span>
          <span className="check-count">{count}</span>
          {selfAgreementOnly(payload) && <span className="check-count check-count-note">{SELF_ONLY}</span>}
        </div>
        {/* One wrapping group: at 1280x800 the pair drops under the headline instead of clipping. */}
        {(onCompare || onRecheck) && (
          <div className="check-verdict-actions">
            {onCompare && <Button variant="quiet" onClick={onCompare}>Compare with datasheet</Button>}
            {onRecheck && <Button variant="primary" onClick={onRecheck}>Re-check</Button>}
          </div>
        )}
      </div>
      {notice}
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
      {firstProblem !== undefined && <h3 className="check-section">Findings</h3>}
      {groups.length > 1 && (
        <div className="check-filter">
          <label><span className="kit-sr">Show</span>
            <select value={sev} onChange={(e) => setSev(e.target.value)}>
              <option value="">All</option><option value="violation">NO-GO only</option><option value="warning">Warnings</option><option value="not_scored">Not scored</option>
            </select>
          </label>
          {parts.length > 0 && (
            <label><span className="kit-sr">Part</span>
              <select value={part} onChange={(e) => setPart(e.target.value)}>
                <option value="">Any part</option>
                {parts.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>
          )}
          {(sev || part) && <><span className="check-filter-count">Showing {shown} of {groups.length}</span><Button variant="quiet" onClick={() => { setSev(""); setPart(""); }}>Clear</Button></>}
        </div>
      )}
      <FindingList selected={groupOf(groups, selected)} onSelect={(g) => onSelect(g === null ? null : groups[g]!.members[0]!)} items={groups.flatMap((g, gi) => (keep(g) ? [{ ...groupItem(payload.cards, g, selected, onSelect, onJump, payload.cli_line, onSwap), index: gi }] : []))} quietSummary={(n) => `Not scored (${n})`} />
      {accepted.length > 0 && (
        // Accepted on record: muted, never red, never hidden.
        <Disclosure summary={`Accepted (${accepted.length})`}>
          {accepted.map((a, i) => <p key={i} className="check-note">{a.title}: {a.reason}. Signed by {a.witness}, {a.date}</p>)}
        </Disclosure>
      )}
      {noteCard && <p className="check-note">{noteWords(noteCard)}</p>}
      {(payload.requirements ?? []).length > 0 && <Requirements rows={payload.requirements!} />}
      {more}
    </Panel>
  );
}

/** The mode header ("Stall · 194.4 W of 60 W") from the card's typed figures; null without them. */
const headOf = (card: Card) => modeHeads([{ observed: card.raw, observed_quantity: card.observed, expected_quantity: card.expected }], [card.targets.find((t) => t.kind === "rail")?.label])?.[0];

/** One card opened: with typed numbers, the mode header, one Fix line, and the figures behind
 * "Show the exact finding"; without, the figures lead as before. */
/** A servo-stall card (page.py's own test: the effort shape, a stall torque expected): its joint
 * and the exact model-file edit from the finding's action line (`fix` is reworded). */
export function servoStall(card: Card): { joint: string; edit: string } | null {
  if (card.shape !== "model/joints/*/limits/effort" || !/expected\s.*stall torque/.test(card.raw)) return null;
  const edit = /action\s.*?; (in .+?), then re-lock/.exec(card.raw)?.[1];
  const joint = /^model\/joints\/(.+)\/limits\/effort$/.exec(card.quantity ?? "")?.[1]; // a joint name may contain "/"
  return edit && joint ? { joint, edit } : null;
}

/** Swap opens the panel; "Set limit" only shows the edit to make in the model file (the app never writes it). */
function ServoActions({ joint, edit, onSwap }: { joint: string; edit: string; onSwap?: (joint: string) => void }) {
  const [shown, setShown] = useState(false);
  return (
    <div className="check-servo">
      <div className="check-verdict-actions">
        {onSwap && <Button variant="primary" onClick={() => onSwap(joint)}>Swap</Button>}
        <Button variant="quiet" onClick={() => setShown(true)}>{"Set limit to servo's stall"}</Button>
      </div>
      {shown && (
        <p className="check-edit">
          <code>{edit}</code>
          <Button variant="quiet" onClick={() => void navigator.clipboard?.writeText(edit)}>Copy</Button>
        </p>
      )}
    </div>
  );
}

const NO_NUMBERS = /^Numbers not shown/;

function Detail({ card, onJump, cli, bare, onSwap }: { card: Card; onJump?: OnJump; cli?: string | null; bare?: boolean; onSwap?: (joint: string) => void }) {
  const servo = servoStall(card);
  // The "i" opens the exact finding, where the note is written out.
  const [exact, setExact] = useState(false);
  // "Numbers not shown" is filler: say it only when nothing else is there to read.
  const note = card.figure_note && (!NO_NUMBERS.test(card.figure_note) || (!card.observed && !card.expected && !card.body)) ? card.figure_note : null;
  const info = note ? <button type="button" className="finding-info" aria-label={note!} title={note!} onClick={() => setExact(true)}>i</button> : null;
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
  // A found fault with no drawn figures states its measured and needed values in the open: no tap.
  const showBody = !!card.body && !card.observed && !card.expected && card.severity !== "not_scored";
  const body = (
    <div className="check-detail">
      <LimitBar card={card} />
      <FindingDetail rows={head ? [fix, ...instead] : [...figures, ...instead, fix]} />
      {showBody && <p className="finding-body">{card.body}</p>}
      {servo && <ServoActions joint={servo.joint} edit={servo.edit} onSwap={onSwap} />}
      <Disclosure summary="Show the exact finding" open={exact || undefined}>
        {head && <FindingDetail rows={figures} />}
        {!card.observed && !card.expected && card.body && !showBody && <p className="finding-body">{card.body}</p>}
        {note && <p className="finding-note">{note}</p>}
        <Disclosure summary="Raw output"><pre className="finding-raw">{card.raw}</pre></Disclosure>
        {cli && <CommandLine command={cli} />}
      </Disclosure>
    </div>
  );
  return head && !bare ? <ModeBlocks heads={[head]} details={[body]} /> : body;
}

/** Value against its limit: width = value/limit (drawn at most to twice the limit), the part past the limit in red.
 * Needs both typed figures in one unit and a positive limit; otherwise nothing is drawn. */
export function barOf(card: Pick<Card, "observed" | "expected"> & { raw?: string }) {
  // A finding without typed figures (the servo-stall one) still states "observed 0.40 N*m" / "expected <= 0.226 N*m" in its own lines.
  const read = (word: string) => { const m = new RegExp(`\\b${word}\\s+(?:<=\\s*)?(\\d+(?:\\.\\d+)?)\\s+([^\\s,;]+)`).exec(card.raw ?? ""); return m ? { value: Number(m[1]), unit: m[2]! } : null; };
  const o = card.observed ?? read("observed"), e = card.expected ?? read("expected");
  if (!o || !e || o.unit !== e.unit || !(e.value > 0) || !Number.isFinite(o.value) || o.value < 0) return null;
  // "expected >= N" is a floor (a servo's torque against what the joint needs): short of it is the bad side.
  const floor = /expected\s+>=/.test(card.raw ?? "");
  if (floor && !(o.value > 0)) return null;
  const ratio = floor ? e.value / o.value : o.value / e.value;
  const drawn = Math.min(ratio, 2), good = ratio > 1 ? 1 / drawn : ratio, fill = ratio > 1 ? 1 : ratio;
  const n = (v: number) => String(+v.toPrecision(3));
  const words = floor ? `${n(o.value)} ${o.unit} available against ${n(e.value)} ${e.unit} needed` : `${n(o.value)} ${o.unit} against a limit of ${n(e.value)} ${e.unit}`;
  return { ratio, good, fill, words: words.trim(), over: floor ? "short by " : "over by ", pct: Math.round(floor ? (1 - 1 / ratio) * 100 : (ratio - 1) * 100) };
}

function LimitBar({ card }: { card: Card }) {
  const b = barOf(card);
  if (!b) return null;
  const over = b.ratio > 1;
  const bg = `linear-gradient(to right, var(--go) ${b.good * 100}%, var(--bad) 0 ${b.fill * 100}%, var(--line) 0)`;
  return (
    <div className="check-limit" data-ratio={b.ratio.toFixed(3)}>
      <p className={`check-limit-words${over ? " is-over" : ""}`}>{b.words}{over ? `, ${b.over}${b.pct}%` : ""}</p>
      <i className={`conn-bar${over ? " is-over" : ""}`} style={{ background: bg, height: 6, display: "block" }} aria-hidden="true" />
    </div>
  );
}

const REQUIREMENT_WORDS = { proven: "Proven", unproven: "Unproven: no check proves it yet", failing: "Failing" } as const;

/** Pro: each plan requirement with the check behind it. A requirement no check covers says so. */
function Requirements({ rows }: { rows: RequirementRow[] }) {
  return (
    <div className="check-requirements">
      <h3 className="check-section">Requirements</h3>
      {rows.map((r) => (
        <p key={r.id} className={`check-note check-req check-req-${r.status}`} title={r.why}>
          <b>{r.text}</b>: {REQUIREMENT_WORDS[r.status]}
        </p>
      ))}
    </div>
  );
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

/** A weak-servo card's own title is "<Part>: <numbers>", its row's title (one line per joint, no shared note). */
const rowTitled = (c: Card) => !!c.group_title && c.label === "Servo too weak";

const groupCards = (cards: Card[]): Group[] => {
  const byKey = new Map<string, Group>();
  const out: Group[] = [];
  cards.forEach((c, i) => {
    // A rail's budget and current-limit cards are one fact (the engine's cluster_root): one row.
    const key = c.cluster_root ? `${c.severity}|cluster|${c.cluster_root}` : `${c.severity}|${c.shape}|${c.group_title || noMode(c.title)}`;
    const hit = byKey.get(key);
    if (hit) hit.members.push(i);
    else { const g = { members: [i] }; byKey.set(key, g); out.push(g); }
  });
  return out;
};

/** Plain words, one rule: a red count for faults, an amber count for warnings, then the rest ("1 fault · 1 warning · 5 not scored"). */
const countWords = (groups: Group[], cards: Card[]) => {
  const n = (sev: string) => groups.filter((g) => cards[g.members[0]!]!.severity === sev).length;
  const [faults, warnings] = [n("violation"), n("warning")];
  const quiet = groups.length - faults - warnings;
  const fault = faults ? <span className="check-count-fault">{faults} {faults === 1 ? "fault" : "faults"}</span> : null;
  const warn = warnings ? <span className="check-count-warn">{warnings} {warnings === 1 ? "warning" : "warnings"}</span> : null;
  return { problems: fault || warn ? [fault, warn] : ["0 findings"], quiet: quiet ? `${quiet} not scored` : "" };
};

const groupOf = (groups: Group[], selected: number | null) =>
  selected === null ? null : groups.findIndex((g) => g.members.includes(selected));

function groupItem(cards: Card[], g: Group, selected: number | null, onSelect: (index: number | null) => void, onJump?: OnJump, cli?: string | null, onSwap?: (joint: string) => void): FindingItem {
  const first = cards[g.members[0]!]!;
  const item = groupRow(cards, g, selected, onSelect, onJump, cli, onSwap);
  // A not-compared row offers its next step: Declare it (part rows too).
  const declare: Jump = { path: first.quantity, targets: first.targets, declare: true };
  // The datasheet-mass result reads as the comparison it is, and offers Declare it (mockup 3B).
  const mass = first.quantity === MASS_NOMINAL ? massLine(first) : null;
  if ((first.severity !== "not_scored" && !mass) || !onJump?.can(declare)) return item;
  return { ...item, title: mass || item.title, action: <Button variant="quiet" onClick={() => onJump(declare)}>Declare it</Button> };
}

function groupRow(cards: Card[], g: Group, selected: number | null, onSelect: (index: number | null) => void, onJump?: OnJump, cli?: string | null, onSwap?: (joint: string) => void): FindingItem {
  const first = cards[g.members[0]!]!;
  const onChip = onJump && ((c: Chip) => onJump(chipJump(c)));
  const severity = first.severity === "violation" ? "bad" : first.severity === "warning" ? "warn" : "muted";
  const className = `finding-card severity-${first.severity}`;
  if (g.members.length === 1) {
    return { key: `${first.shape}-${g.members[0]!}`, severity, title: headOf(first) ? noMode(first.title) : first.title, chips: liveChips(first.whole && severity !== "muted" ? WHOLE : capChips(targetChips(first.targets)), onJump), onChip, className, shape: first.shape, detail: <Detail card={first} onJump={onJump} cli={cli} onSwap={onSwap} /> };
  }
  const heads = modeHeads(g.members.map((i) => ({ observed: cards[i]!.raw, observed_quantity: cards[i]!.observed, expected_quantity: cards[i]!.expected })), g.members.map((i) => cards[i]!.targets.find((t) => t.kind === "rail")?.label));
  const targets = g.members.flatMap((i) => cards[i]!.targets).filter((t, n, all) => all.findIndex((u) => u.id === t.id && u.kind === t.kind) === n);
  const merged = !first.group_title && heads && (!!first.cluster_root || g.members.some((i) => noMode(cards[i]!.title) !== cards[i]!.title));
  if (merged) {
    return { key: `${first.shape}-${g.members[0]!}`, severity, title: noMode(first.title), chips: liveChips(capChips(targetChips(targets)), onJump), onChip, className, shape: first.shape, detail: <ModeBlocks heads={heads} details={g.members.map((i) => <Detail key={i} card={cards[i]!} onJump={onJump} cli={cli} onSwap={onSwap} bare />)} /> };
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
        {!rowTitled(first) && <p className="check-group-note">{first.title}</p>}
        {g.members.map((i) => (
          <div key={i}>
            <FindingRow severity={severity} title={rowTitled(cards[i]!) ? cards[i]!.title : cards[i]!.targets[0]?.label ?? cards[i]!.label} chips={liveChips(targetChips(cards[i]!.targets.slice(1)), onJump)} selected={selected === i} onSelect={() => onSelect(selected === i ? g.members[0]! : i)} onChip={onChip && ((c) => onChip(c as Chip))} />
            {selected === i && <Detail card={cards[i]!} onJump={onJump} cli={cli} onSwap={onSwap} />}
          </div>
        ))}
      </div>
    ),
  };
}

/** The verdict card while the findings are still being worked out: never a stale or a guessed verdict. */
export function CheckPending() {
  return (
    <Panel>
      <div className="check-verdict check-verdict-muted">
        <div className="check-headline verdict-pending" role="status">
          <span className="check-verdict-kicker">Verdict</span>
          <span className="check-verdict-word">Checking…</span>
        </div>
      </div>
    </Panel>
  );
}
