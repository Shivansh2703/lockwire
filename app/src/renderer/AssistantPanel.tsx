import { useEffect, useRef, useState, type ReactNode } from "react";

import { PRODUCT_NAME } from "../shared/branding";
import type { Card as CheckCard, DeclareFormPayload, DeclareRow } from "../shared/api.gen";
import { shownDiff, type AssistantComparison, type AssistantContext, type AssistantFindingChange, type AssistantProposal, type AssistantSettings, type AssistantSetRequest, type AssistantTurn } from "../shared/ipc-contract";
import { inFolder } from "../shared/paths";
import type { CheckFacePayload } from "./checkFacePayload";
import { VERDICT } from "./FindingList";
import { Button, Disclosure, Field, Icon, IconButton, Notice, Panel, StatusPill, type Status } from "./kit";

/** One finding of a turn's check, in the Check screen's own words; `finding` is the engine's path, kept only to ask for it to be shown. */
type Row = { severity: Status; title: string; line: string; finding: string };
type Turn = { who: "you" | "assistant"; text: string; /** What the Assistant changed on screen in this turn (its show tools). */ shown?: string[]; rows?: Row[]; ungrounded?: string[]; /** Verdict words only a proposal supports: the robot after the change. */ proposedOnly?: string[]; /** The cards this answer proposed, by id: each sits under its own answer. */ cards?: string[] };
/** `result`: "Applied" or the refusal line. `sides`: its Before and After lines as the page named them when the card came (an applied fix takes its finding off the page). */
type Card = AssistantProposal & { result?: string; sides?: Sides; /** Its title and change line as they read when it came: after Apply the file holds the new value. */ words?: { title: string; change?: string }; /** Main's sentence when Apply re-checked the card and wrote nothing: its Before and After were replaced. */ recheck?: string };
/** The engine's word from the newest turn's check, with its counts and when main took it (ISO time). */
type Verdict = { word: string; reason?: string; faults: number; warnings: number; at?: string };
/** One robot's conversation. App holds it, so closing the panel (which unmounts it) loses nothing. */
export type Chat = { thread: Turn[]; cards: Card[]; busy: string | null; verdict?: Verdict; /** ISO time of the last Apply: a turn's verdict taken before it is old. */ appliedAt?: string; /** A button's question (main's own words) waiting to be sent: sent once, when the panel can send. */ ask?: string }; // busy: the tool in progress, "" while thinking
export const EMPTY_CHAT: Chat = { thread: [], cards: [], busy: null };
/** The page's own check, as App already holds it: the pill before the first question, and the plain words for a finding. */
export type PageCheck = Pick<CheckFacePayload, "verdict" | "violations" | "warnings" | "cards">;

/** A proposal's dry-run diff, old lines red, new lines green. */
export function Diff({ diff }: { diff: string }) {
  return <pre className="assistant-diff">{diff.split("\n").filter(Boolean).map((l, i) => <span key={i} data-line={/^(\+\+\+|---)/.test(l) ? "head" : l[0] === "+" ? "add" : l[0] === "-" ? "del" : "ctx"}>{l}</span>)}</pre>;
}

// --- a finding in plain words: the page's card for it, never the engine's path ---

const SEVERITY: Record<string, Status> = { violation: "bad", warning: "warn" };
const UNLISTED: Record<string, string> = { violation: "A violation", warning: "A warning" };
/** The page's card for a finding path: a card keeps its first finding's path, so its raw text is searched too (as main's show-only does). */
const cardOf = (cards: CheckCard[], path: string) => cards.find((c) => c.quantity === path) ?? cards.find((c) => c.raw.includes(path));
const plain = (cards: CheckCard[], f: { quantity: string; severity: string }) => cardOf(cards, f.quantity)?.title ?? UNLISTED[f.severity] ?? "A note";
/** The start of a card's text up to its two numbers: one sentence, or two when the first holds only one number. */
const twoNumbers = (body: string) => {
  const [first = "", second] = body.split(/(?<=\.)\s+(?=[A-Z])/);
  return second && (first.match(/\d+(?:\.\d+)?/g) ?? []).length < 2 ? `${first} ${second}` : first;
};
/** The faults and warnings of a turn's check as rows, one per card. */
function rowsOf(findings: NonNullable<AssistantTurn["findings"]>, cards: CheckCard[]): Row[] {
  const seen = new Set<CheckCard>();
  return findings.flatMap((f) => {
    const card = cardOf(cards, f.quantity);
    if (!SEVERITY[f.severity] || (card && seen.has(card))) return [];
    if (card) seen.add(card);
    return [{ severity: SEVERITY[f.severity]!, title: plain(cards, f), line: card ? twoNumbers(card.body) : "", finding: f.quantity }];
  });
}

// --- the verdict pill: the engine's word and its count, never the model's ---

const count = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;
// ponytail: "just now" is worked out when the panel draws, with no timer; add one if a stale minute count is ever seen.
const when = (at: string) => { const min = Math.floor((Date.now() - Date.parse(at)) / 60_000); return min < 1 ? "just now" : `${min} min ago`; };
function VerdictPill({ v }: { v: Verdict }) {
  if (v.word === "unknown") return <span className="assistant-verdict"><StatusPill status="muted">Not checked</StatusPill><small>{v.reason}</small></span>;
  const [status, label] = VERDICT[v.word] ?? ["muted", v.word];
  const n = v.faults ? count(v.faults, "violation") : v.warnings ? count(v.warnings, "warning") : "";
  return <span className="assistant-verdict"><StatusPill status={status}>{label}{n && ` · ${n}`}</StatusPill><small>from the check{v.at ? `, ${when(v.at)}` : ""}</small></span>;
}

// --- the answer: what main could not ground is marked, the words are never changed ---

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// Main's own edges (assistantKnowhow.ts): a number may follow a range's dash, a verdict word may not (the GO inside NO-GO is not a GO).
// ponytail: only the two usual spellings of a verdict written with spaces are found; "Go With Notes" stays unmarked.
const SPACED: Record<string, string> = { GO_WITH_NOTES: "GO with notes", NOT_SCORED: "NOT SCORED" };
const AFTER = "after the proposed change, not the robot now";
function Answer({ text, ungrounded, proposed }: { text: string; ungrounded?: string[]; proposed?: string[] }) {
  const spelled = (list?: string[]) => (list ?? []).flatMap((u) => (SPACED[u] ? [u, SPACED[u]] : [u]));
  const after = new Set(spelled(proposed)), unverified = new Set(spelled(ungrounded));
  const items = [...new Set([...unverified, ...after])].sort((a, b) => b.length - a.length);
  if (!items.length) return <>{text}</>;
  const find = new RegExp(`(${items.map((u) => (/^[-−\d]/.test(u) ? `(?<![\\w.])${esc(u)}(?![A-Za-z])` : `(?<![\\w-])${esc(u)}(?![\\w-])`)).join("|")})`, "g");
  // The same dotted mark for both: an unverified word, or one only a proposal supports, whose words say which.
  const why = (part: string) => (after.has(part) && !unverified.has(part) ? AFTER : "unverified");
  return <>{text.split(find).map((part, i) => (i % 2 ? <span key={i} className="assistant-unverified" title={why(part)}>{part}<span className="kit-sr"> ({why(part)})</span></span> : part))}</>;
}

// --- a card: the change in plain words, the engine's before and after, one Apply ---

const TITLE: Record<string, string> = {
  add: "Add a part", review: "Mark a value as reviewed", place: "Place a part", move: "Move a part", rotate: "Turn a part",
  reparent: "Attach a part somewhere else", "mount-shape": "Change how a part is mounted", "build robot": "Build the robot",
};
/** A value the page has no declare row for (a bare number, not a quantity): its words and unit. Anything else reads as its field name. */
const FIELD: Record<string, [string, string]> = { awg: ["wire gauge", "AWG"] };
/** The page's name for a part, wire or rail id (its robot graph's label), else the id. */
type Labels = Record<string, string>;
const reEsc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/** The engine's sentence with each dotted id (rail.5v, dev.jetson, w.pack) the page has a name for written as that name. */
const plainIds = (text: string, labels: Labels) =>
  Object.keys(labels).filter((id) => id.includes(".")).sort((a, b) => b.length - a.length).reduce((t, id) => t.replace(new RegExp(`(?<![\\w.-])${reEsc(id)}(?![\\w-])`, "g"), labels[id]!), text);
/** A card's title and its change in one line, from its own change: never the command's name, an id or a path. */
function describe(c: Card, rows: DeclareRow[], labels: Labels = {}): { title: string; change?: string } {
  const a = c.args;
  if (c.command === "swap") return { title: `Swap the ${String(a.joint).replaceAll("_", " ")} servo`, change: c.swap?.from ? `${c.swap.from.name} → ${c.swap.to.name}` : `New servo: ${c.swap?.to.name ?? String(a.part_id)}` };
  if (c.command === "add" && c.cites) return { title: TITLE.add!, change: String((a.fields as Record<string, unknown> | undefined)?.model ?? a.id) };
  if (c.command !== "declare") return { title: TITLE[c.command] ?? "Change the robot file" };
  const row = rows.find((r) => r.path === a.path);
  const numeric = /^[-\d.]+$/.test(String(a.value));
  if (!row) { // the same words from the path itself: its owner's name on the page, the field's words, and the old value from the engine's own dry run
    const [id = "", field = ""] = String(a.path).split("/").slice(-2);
    const [words, u] = FIELD[field] ?? [field.replaceAll("_", " "), ""];
    const unit = u && numeric ? ` ${u}` : "";
    const was = c.diff.match(new RegExp(`^-\\s+${reEsc(field)}:\\s*(.+?)\\s*$`, "m"))?.[1];
    // The owner's name: the robot graph's label, else the group a declare row of the same part shows; the id only when the page has neither.
    // ponytail: a part with neither would need the engine to name it on the dry run (page.human_name(short_name(id)) does it for the graph).
    const owner = labels[id] ?? rows.find((r) => r.path.includes(`/${id}/`))?.group.split(" · ")[0] ?? id;
    const title = `${was ? "Change" : "Set"} ${words} on ${owner}`;
    return { title, change: was ? `${was}${unit} → ${String(a.value)}${unit}` : `New value: ${String(a.value)}${unit}` };
  }
  const unit = row.unit ? ` ${row.unit}` : "";
  const to = `${String(a.value)}${numeric ? unit : ""}`;
  if (row.value === null) return { title: `Set ${row.short.toLowerCase()} on ${row.group}`, change: `New value: ${to}` };
  return { title: `Change ${row.short.toLowerCase()} on ${row.group}`, change: `${[row.value].flat().join(", ")}${unit} → ${to}` };
}
/** The page's names plus a swap card's two servos by their catalog names (the Check screen's sentence names the old one by its id). */
const withServos = (labels: Labels, c: AssistantProposal): Labels => ({ ...labels, ...Object.fromEntries([c.swap?.from, c.swap?.to].flatMap((s) => (s ? [[s.id, s.name]] : []))) });

const SHOWN_CHANGES = 6;
const NOT_COMPARED = `${PRODUCT_NAME} could not compare before and after.`;
const needs = (f: AssistantFindingChange) => (f.expected ? `, needs ${f.expected}` : "");
type Sides = { before: string[]; after: string[] };
/** The Before and After lines of a comparison in the Check screen's own words: a finding the page has a card for reads as that card's title (and its
 * typed observed figure when it has one; never its body sentence, which carries the engine's words); the engine's own sentence only where the page has no card, or for the changed robot, with ids as the page names them. */
function sidesOf(c: AssistantComparison, cards: CheckCard[], labels: Labels): Sides | undefined {
  if ("error" in c) return undefined;
  const plainly = (s: string) => plainIds(s, labels);
  const shown = c.changes.slice(0, SHOWN_CHANGES).map((f) => {
    const card = cardOf(cards, f.quantity);
    const name = card?.title ?? plain(cards, f);
    const now = card ? `${card.title}${card.observed ? `: ${card.observed.value} ${card.observed.unit}` : ""}` : `${name}: ${plainly(`${f.was ?? f.observed}${f.kind === "cleared" ? needs(f) : ""}`)}`;
    return { f, name, now };
  });
  return {
    before: shown.filter(({ f }) => f.kind !== "new").map(({ now }) => now),
    after: shown.map(({ f, name }) => (f.kind === "cleared" ? `${name}: cleared` : `${f.kind === "new" ? "New: " : ""}${name}: ${plainly(`${f.observed}${needs(f)}`)}`)),
  };
}
/** What the engine's check says a proposal does (main/proposalCheck.ts): its verdict before and after, side by side, with the findings that move. */
function BeforeAfter({ c, sides }: { c: AssistantComparison; sides?: Sides }) {
  if ("error" in c || !sides) return <span className="assistant-note">{NOT_COMPARED}</span>;
  const side = (label: string, verdict: string, lines: string[]) => {
    const [status, word] = VERDICT[verdict] ?? ["muted", verdict];
    return <div className="assistant-side"><small>{label}</small><StatusPill status={status as Status}>{word}</StatusPill>{lines.map((l, i) => <span key={i}>{l}</span>)}</div>;
  };
  return (
    <>
      <div className="assistant-sides">
        {side("Before", c.before, sides.before)}
        {side("After", c.after, sides.after)}
      </div>
      {!c.changes.length && <span className="assistant-note">Nothing changes in the check.</span>}
      {c.changes.length > SHOWN_CHANGES && <span className="assistant-note">and {c.changes.length - SHOWN_CHANGES} more</span>}
    </>
  );
}

/** The one consent: a key when none is saved, and this robot, in one tap. */
export function AllowScreen({ contract, hasKey, files, onSaved }: { contract: string; hasKey: boolean; /** The file tools are in the tool table. */ files?: boolean; onSaved: (s: AssistantSettings) => void }) {
  const [key, setKey] = useState("");
  const [error, setError] = useState("");
  const allow = async () => {
    const r = await window.lockwire.setAssistantSettings(hasKey ? { enableRobot: contract } : { key: key.trim(), enableRobot: contract });
    setError(r.error ?? "");
    onSaved(r.settings);
  };
  return (
    <div className="assistant-col">
      {!hasKey && <Field label="Paste your Anthropic key" type="password" value={key} onChange={(e: { target: { value: string } }) => setKey(e.target.value)} />}
      {!hasKey && <span className="assistant-note">The Assistant reads your robot and proposes changes you apply. It needs your own Anthropic key (get one at console.anthropic.com). The key is saved encrypted on this computer and sent only to Anthropic.</span>}
      <span className="assistant-note">The Assistant can use this robot's check results{files ? " and files in this robot's folder" : ""}.</span>
      <span className="assistant-note">Your robot's contract is sent to Anthropic with your key.</span>
      {error && <Notice kind="error">{error}</Notice>}
      <Button variant="primary" disabled={!hasKey && !key.trim()} onClick={() => void allow()}>{hasKey ? "Allow for this robot" : "Save key and allow for this robot"}</Button>
    </div>
  );
}

/** A sensible model so nobody hand-types an id; the field still takes any other. */
const DEFAULT_MODEL = "claude-sonnet-5-5";
const VIEW = { graph: "Graph", power: "Power connections", data: "Data connections", mechanical: "Mechanical connections" };
const STEPS = ["Ask", "Explain", "Propose", "Apply"];

// The Assistant: mounted only while open, so a closed one runs nothing and claims no slot.
// Every edit it suggests is a card a person applies with one button; the key only ever goes to main.
export function AssistantPanel({ contract, revision, mode, witness, onName, check, rows = [], labels = {}, context, chat, onChat, onApplied }: {
  contract: string;
  revision: string;
  mode: string;
  /** The app's saved name, as the page carries it (the engine marks a saved one "(default)"); "" when none is saved yet. Apply records it. */
  witness: string;
  /** The saved name changed: the page's fresh declare form, which carries it. */
  onName?: (form: DeclareFormPayload) => void;
  check?: PageCheck;
  /** The page's declare rows: the plain words for a value a card changes. */
  rows?: DeclareRow[];
  /** The page's name for each id (its robot graph's labels): the plain words for a part, wire or rail a card or the engine's sentence names. */
  labels?: Labels;
  /** The selected part and the open view, sent with each question (the data path only). */
  context?: AssistantContext;
  chat: Chat;
  onChat: (update: (c: Chat) => Chat) => void;
  onApplied: (contract: string) => Promise<void> | void;
}) {
  const [settings, setSettings] = useState<AssistantSettings | null>(null);
  const [model, setModel] = useState("");
  const [text, setText] = useState("");
  const [asking, setAsking] = useState<{ card: string; name: string; error?: string } | null>(null); // the name field: on the card whose first Apply was pressed, or under Advanced (card "") to change the saved name
  const [dropped, setDropped] = useState<string[]>([]); // tags taken off for the next question
  const { thread, cards, busy } = chat;
  const [error, setError] = useState("");
  const sending = useRef<string | null>(null); // events carry no robot id, so only the robot that sent may show them
  const end = useRef<HTMLSpanElement>(null);
  const askBox = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { const t = askBox.current; if (t) { t.style.height = "auto"; t.style.height = `${t.scrollHeight}px`; } }); // one line, grows with the text; the CSS max-height stops it at about six lines
  const pageCards = check?.cards ?? [];
  const here = inFolder(contract); // a full path on screen reads relative to the robot's folder

  useEffect(() => {
    void window.lockwire.getAssistantSettings().then((s) => { setSettings(s); setModel(s.model || DEFAULT_MODEL); });
    return window.lockwire.onAssistantEvent((e) => { if (e.type === "tool" && sending.current === contract) onChat((c) => ({ ...c, busy: e.name })); });
  }, [contract]); // onChat closes over the path, so follow it
  useEffect(() => { end.current?.scrollIntoView?.({ block: "end" }); }, [thread.length, cards.length, busy, context?.selection?.id, context?.view]); // braces: a newer browser's scroll returns a promise, which an effect must not return

  const tags = [
    ...(context?.selection ? [{ key: `selection:${context.selection.id}`, label: context.selection.name }] : []),
    ...(context?.view ? [{ key: `view:${context.view}`, label: VIEW[context.view] }] : []),
  ].filter((t) => !dropped.includes(t.key));
  const save = async (request: AssistantSetRequest) => {
    const r = await window.lockwire.setAssistantSettings(request);
    setSettings(r.settings);
    setError(r.error ?? "");
    return !r.error;
  };
  const send = async (asked?: string) => {
    const message = (asked ?? text).trim();
    if (!message || !model.trim() || busy !== null) return;
    if (model.trim() !== settings?.model && !(await save({ model: model.trim() }))) return; // the user's own model
    const sent = { ...(tags.some((t) => t.key.startsWith("selection:")) ? { selection: context!.selection } : {}), ...(tags.some((t) => t.key.startsWith("view:")) ? { view: context!.view } : {}) };
    onChat((c) => ({ ...c, thread: [...c.thread, { who: "you", text: message }], busy: "" }));
    if (asked === undefined) setText("");
    setError(""); setDropped([]);
    sending.current = contract;
    try {
      const turn = await window.lockwire.sendAssistant({ contract, revision, mode, text: message, ...(Object.keys(sent).length ? { context: sent } : {}) });
      if (turn.error) setError(turn.error);
      // The verdict and the findings are the engine's, from this turn's check: the model's words are never read for either.
      const found = turn.findings ?? [];
      const verdict = turn.verdict && { ...turn.verdict, faults: found.filter((f) => f.severity === "violation").length, warnings: found.filter((f) => f.severity === "warning").length };
      const proposed: Card[] = turn.proposals.map((p) => { const sides = p.comparison && sidesOf(p.comparison, pageCards, withServos(labels, p)); return { ...p, words: describe(p, rows, labels), ...(sides ? { sides } : {}) }; });
      const answer: Turn = { who: "assistant", text: turn.text, ...(turn.shown?.length ? { shown: turn.shown } : {}), ...(turn.findings ? { rows: rowsOf(found, pageCards) } : {}), ...(turn.ungrounded?.length ? { ungrounded: turn.ungrounded } : {}), ...(turn.proposedOnly?.length ? { proposedOnly: turn.proposedOnly } : {}), ...(proposed.length ? { cards: proposed.map((p) => p.id) } : {}) };
      onChat((c) => ({ ...c, thread: turn.error ? c.thread : [...c.thread, answer], cards: [...c.cards, ...proposed], ...(verdict && !(c.verdict?.at && verdict.at < c.verdict.at) && !(c.appliedAt && verdict.at < c.appliedAt) ? { verdict } : {}) })); // of two turns that overlap, the verdict taken last stays; one taken before the last Apply never shows
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { if (sending.current === contract) sending.current = null; onChat((c) => ({ ...c, busy: null })); }
  };
  // A button's question is sent once the panel can send (key saved, this robot allowed, nothing in flight), never twice.
  const asked = useRef<string | null>(null);
  const canSend = !!settings?.hasKey && settings.enabledRobots.includes(contract) && busy === null && !!model.trim();
  useEffect(() => {
    if (!chat.ask) { asked.current = null; return; }
    if (!canSend || asked.current === chat.ask) return;
    asked.current = chat.ask;
    onChat((c) => ({ ...c, ask: undefined }));
    void send(chat.ask);
  }, [chat.ask, canSend]); // eslint-disable-line react-hooks/exhaustive-deps -- send reads this render's settings
  // Apply is only ever this click. The name is the app's saved one; with none saved, the first Apply asks for it once and saves it where the app keeps it.
  // A card's own apply in flight (its re-check takes about 2 s): its Apply is off until the answer, so a second press cannot ask for a card main is holding.
  const [applying, setApplying] = useState<string[]>([]);
  const flying = useRef(new Set<string>()); // the same, read at once: two presses in one frame
  const apply = async (card: Card, name: string) => {
    if (flying.current.has(card.id)) return;
    flying.current.add(card.id);
    setApplying((a) => [...a, card.id]);
    try { await applyNow(card, name); } finally { flying.current.delete(card.id); setApplying((a) => a.filter((id) => id !== card.id)); }
  };
  const applyNow = async (card: Card, name: string) => {
    const r = await window.lockwire.applyAssistant({ id: card.id, witness: name });
    if (!r.ok && r.comparison) { // re-checked at Apply: nothing was written; the card shows the new Before and After and stays open (its proof was for the robot as it was)
      const fresh = r.comparison;
      onChat((c) => ({ ...c, cards: c.cards.map((x) => (x.id === card.id ? { ...x, comparison: fresh, sides: sidesOf(fresh, pageCards, withServos(labels, x)), recheck: r.error, proved: undefined } : x)) }));
      return;
    }
    const result = r.ok ? "Applied" : r.error.includes("revision") ? "The robot file changed. Ask again." : r.error;
    onChat((c) => ({ ...c, cards: c.cards.map((x) => (x.id === card.id ? { ...x, result } : x)), ...(r.ok ? { verdict: undefined, appliedAt: new Date().toISOString() } : {}) })); // after a write the last turn's verdict is old: the page's own check shows
    if (r.ok) await onApplied(contract);
  };
  /** Saves the typed name where the app keeps it (the engine's set-witness); the saved name, or null with the reason on the field. */
  const saveName = async (): Promise<string | null> => {
    const reply = await window.lockwire.writeContract({ command: "set-witness", args: { name: asking!.name.trim() } });
    const refusal: unknown = Reflect.get(reply ?? {}, "refusal");
    if (typeof refusal === "string") { setAsking({ ...asking!, error: refusal }); return null; }
    const form = await window.lockwire.getDeclareForm({ contract });
    onName?.(form);
    if (!form.witness.prefill.trim()) { setAsking({ ...asking!, error: "Your name was not saved. Try again." }); return null; }
    setAsking(null);
    return form.witness.prefill;
  };
  const saveNameThenApply = async (card: Card) => { const saved = await saveName(); if (saved) await apply(card, saved); };
  const nameField = (onSave: () => void) => (
    <form className="assistant-row assistant-name" onSubmit={(e) => { e.preventDefault(); onSave(); }}>
      <Field label="Your name, recorded with every change" autoFocus value={asking!.name} error={asking!.error} onChange={(e: { target: { value: string } }) => setAsking({ card: asking!.card, name: e.target.value })} />
      <Button type="submit" variant="primary" disabled={!asking!.name.trim()}>Save</Button>
    </form>
  );

  if (!settings) return <Panel title="Assistant"><span className="assistant-note">Loading</span></Panel>;
  const enabled = settings.enabledRobots.includes(contract);
  if (!settings.hasKey || !enabled) return <Panel title="Assistant"><AllowScreen contract={contract} hasKey={settings.hasKey} files={settings.files} onSaved={setSettings} /></Panel>;
  const verdict: Verdict | undefined = chat.verdict ?? (check && { word: check.verdict, faults: check.violations, warnings: check.warnings });
  const name = witness.replace(/ \(default\)$/, "").trim();
  const answers = thread.filter((t) => t.who === "assistant");
  const repeated = (t: Turn) => JSON.stringify(answers[answers.indexOf(t) - 1]?.rows) === JSON.stringify(t.rows); // the same findings are listed once, not under every answer
  const card = (c: Card) => {
    const { title, change } = c.words ?? describe(c, rows, labels);
    return (
      <div key={c.id} className="assistant-card">
        <div className="assistant-card-head"><strong>{title}</strong>{c.proved && <StatusPill status="go">Proved by the engine</StatusPill>}</div>
        {change && <span>{change}</span>}
        {c.cites?.map((n, i) => <span key={i}>{n.name.replaceAll("_", " ")}: {n.value} {n.unit}, p.{n.page}, “{n.quote}”</span>)}
        {c.note && <span className="assistant-note">{c.note}</span>}
        {c.comparison && <BeforeAfter c={c.comparison} sides={c.sides ?? sidesOf(c.comparison, pageCards, withServos(labels, c))} />}
        {c.recheck && !c.result && <span className="assistant-note" role="status">{c.recheck}</span>}
        <Disclosure summary="The exact change"><Diff diff={here(shownDiff(c.diff, name))} /></Disclosure>
        {c.result ? <span className="assistant-note">{here(c.result)}</span> : asking?.card === c.id ? nameField(() => void saveNameThenApply(c)) : (
          <>
            <div className="assistant-row">
              <Button variant="primary" disabled={busy !== null || applying.includes(c.id)} onClick={() => (name ? void apply(c, witness) : setAsking({ card: c.id, name: "" }))}>Apply</Button>
              <Button variant="quiet" onClick={() => onChat((s) => ({ ...s, cards: s.cards.filter((x) => x.id !== c.id) }))}>Dismiss</Button>
              {name && c.command !== "swap" && <small className="assistant-as">as {name}</small>}
            </div>
            <span className="assistant-note">Nothing changes until you press Apply.</span>
          </>
        )}
      </div>
    );
  };
  const footer = (
    <div className="assistant-col">
      {tags.length > 0 && (
        <div className="assistant-tags">
          {tags.map((t) => <span key={t.key} className="assistant-tag">{t.label}<button type="button" aria-label={`Do not send ${t.label} with this question`} title="Do not send with this question" onClick={() => setDropped((d) => [...d, t.key])}>×</button></span>)}
        </div>
      )}
      <div className="assistant-askbox">
        <textarea ref={askBox} className="kit-control assistant-ask" aria-label="Ask about this robot" placeholder="Ask about this robot" rows={1} value={text} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }} />
        <button type="button" className="assistant-send" aria-label="Send" title="Send (Enter)" disabled={!text.trim() || !model.trim() || busy !== null} onClick={() => void send()}><Icon name="enter" /></button>
      </div>
      {busy !== null && <div className="assistant-row"><Button variant="quiet" onClick={() => void window.lockwire.cancelAssistant()}>Cancel</Button></div>}
      <Disclosure summary="Advanced">
        <Field label="Model" placeholder="Model id, from your console" value={model} onChange={(e: { target: { value: string } }) => setModel(e.target.value)} />
        {asking?.card === "" ? nameField(() => void saveName()) : name && (
          <div className="assistant-row"><small>Applying as {name}</small><Button variant="quiet" onClick={() => setAsking({ card: "", name })}>Change</Button></div>
        )}
        <div className="assistant-row assistant-secondary">
          <Button variant="quiet" onClick={() => { onChat((c) => ({ ...c, ask: undefined })); void save({ disableRobot: contract }); }}>Turn off for this robot</Button>
          <Button variant="quiet" onClick={() => void save({ forgetKey: true })}>Forget key</Button>
        </div>
      </Disclosure>
    </div>
  );
  return (
    <Panel title="Assistant" footer={footer} actions={<>
      {verdict && <VerdictPill v={verdict} />}
      <p className="assistant-steps">{STEPS.flatMap((w, i): ReactNode[] => [...(i ? [<span key={`${w}-sep`} aria-hidden="true">›</span>] : []), <span key={w}>{w}</span>])}</p>
    </>}>
      <div className="assistant-col assistant-thread">
        {thread.flatMap((t, i) => [
          ...(t.rows?.length && !repeated(t) ? [
            <ul key={`${i}-rows`} className="assistant-findings">
              {t.rows.map((r) => (
                <li key={r.finding}>
                  <i className={`kit-dot kit-s-${r.severity}`} aria-hidden="true" />
                  <span><strong><span className="kit-sr">{r.severity === "bad" ? "Violation: " : "Warning: "}</span>{r.title}</strong>{r.line && <small>{r.line}</small>}</span>
                  <IconButton icon="eye" label={`Show ${r.title} on the robot`} disabled={busy !== null} onClick={() => void window.lockwire.showAssistantFinding({ contract, finding: r.finding })} />
                </li>
              ))}
            </ul>,
          ] : []),
          <p key={i} className="assistant-turn" data-who={t.who}>{t.who === "assistant" ? <Answer text={here(t.text)} ungrounded={t.ungrounded} proposed={t.proposedOnly} /> : t.text /* the person's own words stay as typed */}</p>,
          ...(t.shown ? [<span key={`${i}-shown`} className="assistant-note assistant-shown">Showed: {here(t.shown.join(" "))}</span>] : []), // its own line under the answer
          ...cards.filter((c) => t.cards?.includes(c.id)).map(card),
        ])}
        {busy !== null && <span className="assistant-note" role="status">{busy ? "Working" : "Thinking"}</span>}
        {cards.filter((c) => !thread.some((t) => t.cards?.includes(c.id))).map(card) /* a card whose answer never came (a failed turn) belongs to no answer */}
        {error && <Notice kind="error">{here(error)}</Notice>}
        <span ref={end} />
      </div>
    </Panel>
  );
}
