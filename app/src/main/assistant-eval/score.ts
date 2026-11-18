// Scores one Assistant answer against a question's checkable facts: every `all` pattern matches, no `none` pattern does,
// and at least `proposals` cards came back. Patterns are case-insensitive regular expressions.
// The harder exam (questions-hard.json) adds what a turn shows besides its text: the tools it called, the card count and what each
// card's engine comparison cleared, the turn's verdict word and its `ungrounded` list. `engine`, `facts`, `doc`, `model`, `thread`
// and `note` are for verify-questions.ts and the reader; the scorer ignores them.
import { SENTENCE } from "../assistantKnowhow";

import { PRINTED, unitKey, WEIGHT } from "./datasheet";

export const SUBJECTS = ["concepts", "mechanical", "electrical", "testing"] as const;
export type Subject = (typeof SUBJECTS)[number];
export interface EvalQuestion {
  id: string; robot: string; question: string; all?: string[]; none?: string[]; proposals?: number;
  subject?: Subject;
  /** At most this many cards ("still exactly one card" with `proposals: 1`). */
  proposalsMax?: number;
  /** At least this many cards whose engine comparison clears a violation and adds none (in a thread: its cards so far). */
  fixes?: number;
  /** In a thread, count only this turn's cards, not the thread's earlier ones ("And the fuse?" needs a card of its own). */
  fresh?: boolean;
  /** Each pattern must match the name of at least one tool the turn called. */
  tools?: string[];
  /** The turn's verdict word (main's, from the engine's check) must be exactly this. */
  verdict?: string;
  /** A claim in the text matching one of these (not negated just before it) must be in `ungrounded`. */
  marked?: string[];
  /** At most this many `ungrounded` entries (0: every number with a unit came from a tool or the person). */
  ungroundedMax?: number;
  /** At least `n` of these names appear in the text as whole words. */
  atLeast?: { n: number; of: string[] };
  /** The exam's test datasheet (datasheet.ts) is put beside the robot; every card must cite only numbers it prints, on page 1, and carry its note. */
  datasheet?: boolean;
  /** Every card's part id must be one catalog-parts returned in this thread. */
  catalogOnly?: boolean;
  /** Every card's own engine check after the change must say this. */
  cardAfter?: string;
  /** Each pattern must match the diff of at least one card. */
  cardDiff?: string[];
  /** A NOTE, not a failure, when suggest-fixes did not answer in this thread by this turn (the fix loop says to ask it first, but the check's own advice is a right path too). */
  searched?: boolean;
  engine?: { command: string; args?: Record<string, unknown> }[]; facts?: string[]; doc?: string; model?: string; thread?: string; note?: string;
}
/** What the scorer reads of a card (AssistantProposal): the change itself, never the prose about it. */
export interface EvalCard { command: string; args: Record<string, unknown>; diff?: string; note?: string; comparison?: unknown }
/** An engine reply the turn's tools got (live.eval.ts captures catalog-parts and suggest-fixes, in the thread so far). */
export interface EvalReply { command: string; payload: unknown }
export interface EvalAnswer {
  text: string; proposals: number;
  cards?: EvalCard[]; replies?: EvalReply[];
  tools?: string[]; verdict?: string; ungrounded?: string[];
  /** Cards whose engine comparison cleared a violation and added none. */
  fixes?: number;
  /** Cards with no engine comparison at all (it timed out): told apart from a wrong fix in the FAIL line. */
  uncompared?: number;
}

const NEGATED = /\b(?:not|never|no longer|isn['’]t|aren['’]t|wasn['’]t|can['’]t|cannot|won['’]t|doesn['’]t)(?:\s+\w+){0,2}\s*$/i; // assistantKnowhow.ts's rule
// A tool name as a whole word; its hyphen may be written as a space ("robot graph").
const word = (w: string) => new RegExp(`(?<![\\w-])${w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replaceAll("-", "[- ]")}(?![\\w-])`, "i");
// A claim inside a question ("Is it safe to power on?") claims nothing: the app does not mark it either.
const questioned = (text: string, at: number) => /^[^.!?\n]*\?/.test(text.slice(at));

// A GO inside a sentence that also holds the turn's verdict ("It is GO only once you fix the rail; right now it's NO-GO.") claims nothing: the app's rule, its SENTENCE splitter.
const sameSentenceAsVerdict = (text: string, at: number, verdict?: string) => {
  const v = verdict && new RegExp(`(?<![\\w-])${verdict}(?![\\w-])`);
  return !!v && [...text.matchAll(SENTENCE)].some((s) => s.index <= at && at < s.index + s[0].length && v.test(s[0]));
};

// No Markdown on camera: the panel shows plain text (the model is told: no asterisks, no # headings, no backticks, no tables, no links;
// lines that start with "- " are fine). Only paired marks count as asterisks: **bold** or *italic*, never N*m, 3*4 or "x * y".
const MARKDOWN: [RegExp, string][] = [
  [/\*\*(?=\S)[^*\n]+?\*\*/, "**bold**"], [/(?<![\w*])\*(?![\s*])[^*\n]+?(?<![\s*])\*(?![\w*])/, "*italic*"], [/^ {0,3}#/m, "a line starting with #"],
  [/`/, "a backtick"], [/^\s*\|.*\|\s*$/m, "a table row"], [/\[[^\]\n]+\]\([^)\s]+\)/, "a [text](url) link"],
];
export const markdownIn = (text: string): string[] => MARKDOWN.filter(([re]) => re.test(text)).map(([, what]) => what);

const rec = (v: unknown): Record<string, unknown> => (v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {});
const list = (v: unknown): Record<string, unknown>[] => (Array.isArray(v) ? v.map(rec) : []);
/** A value as one token: "7 A", "7A" and "7.0 A" are one; "22" and 22 are one. */
const valueKey = (v: unknown) => { const s = String(v).replace(/\s+/g, ""); const m = /^(-?\d+(?:\.\d+)?)(.*)$/.exec(s); return m ? `${Number(m[1])}${unitKey(m[2]!)}` : s.toLowerCase(); };
/** A card's change as (target, value) pairs: a declare's path and value, a swap's joint and part. Anything else cannot be compared (null). */
const changeOf = (command: unknown, args: unknown): string[] | null => {
  const a = rec(args);
  if (command === "declare") return [`${String(a.path)}=${valueKey(a.value)}`];
  if (command === "swap") return [`swap ${String(a.joint)}=${String(a.part_id)}`];
  return null;
};
/** The numbers a datasheet card cites, from its source ("file: name value unit (p.N: \"quote\"); ..."), as propose-part writes it.
 * The unit holds no digit, so a name such as "torque at 6 V" is not read as a number. */
export const citedNumbers = (source: string): { value: number; unit: string; page: number }[] =>
  [...source.matchAll(/ (-?\d+(?:\.\d+)?) ((?:(?!\d)[^"();])+?) \(p\.(\d+): "[^"]*"\)/g)].map((m) => ({ value: Number(m[1]), unit: m[2]!, page: Number(m[3]) }));

/** The name a datasheet card cites the weight under ("mass 57 g (p.1: ..."), or null when 57 g is not among its cited numbers. */
const weightNamed = (source: string): string | null => {
  const [v, u] = WEIGHT.split(" ");
  return new RegExp(`(?:^|: |; )([^;:"]+?) ${v} ${u} \\(p\\.\\d+: "`).exec(source)?.[1] ?? null;
};

/** Why the cards fail the question's card rules (datasheet, catalogOnly, cardAfter, cardDiff). */
function cardProblems(q: EvalQuestion, cards: EvalCard[], replies: EvalReply[]): string[] {
  const why: string[] = [];
  const catalog = new Set(replies.filter((r) => r.command === "catalog-parts").flatMap((r) => list(rec(r.payload).parts).map((p) => p.id)));
  for (const c of cards) {
    const a = rec(c.args), comparison = rec(c.comparison);
    if (q.datasheet) {
      const cited = citedNumbers(String(a.source ?? ""));
      if (!c.note) why.push("datasheet card carries no note");
      if (!cited.length) why.push(`card cites no number from the datasheet (${c.command})`);
      for (const n of cited) if (n.page !== 1 || !PRINTED.includes(`${n.value} ${unitKey(n.unit)}`)) why.push(`card number ${n.value} ${n.unit} (p.${n.page}) is not printed in the datasheet`);
      const mass = rec(a.fields).physical_mass;
      // the card must carry the datasheet's 57 g: as its declared mass, or as a cited number under any name (a NOTE then says the part declares no mass)
      if (mass === undefined ? !weightNamed(String(a.source ?? "")) : valueKey(mass) !== valueKey(WEIGHT)) why.push(mass === undefined ? `the card carries no ${WEIGHT}` : `card mass ${String(mass)} is not the datasheet's ${WEIGHT}`);
    }
    if (q.catalogOnly && c.command === "add" && !catalog.has(a.id)) why.push(`card part ${String(a.id)} was not returned by catalog-parts in this thread`);
    if (q.cardAfter !== undefined && comparison.after !== q.cardAfter) why.push(`card's engine check says ${String(comparison.after ?? "nothing")}, wanted ${q.cardAfter}`);
  }
  // A declare's or a swap's own change ("params/chassis_length/value=0.6"); any other card's diff (a propose-robot line). Not a declare's engine diff:
  // its context lines and re-wrapped strings can name parts the change does not touch.
  for (const p of q.cardDiff ?? []) if (!cards.some((c) => new RegExp(p, "i").test((changeOf(c.command, c.args) ?? [c.diff ?? ""]).join("\n")))) why.push(`no card's change matches /${p}/`);
  return why;
}
const changeText = (c: EvalCard) => (changeOf(c.command, c.args) ?? [c.command]).join(", ").replace(/=/g, " = ");
/** Every fix suggest-fixes returned in the thread, each as its (target, value) pairs. */
const searchedFixes = (replies: EvalReply[]) => replies.filter((r) => r.command === "suggest-fixes").flatMap((r) => list(rec(r.payload).fixes)).map((f) => list(f.steps).flatMap((st) => changeOf(st.command, st.args) ?? []));
const isSearched = (c: EvalCard, fixes: string[][]) => { const m = changeOf(c.command, c.args); return !!m && fixes.some((f) => m.every((x) => f.includes(x))); };

export function score(q: EvalQuestion, answer: EvalAnswer): { pass: boolean; why: string[]; notes?: string[] } {
  const tools = answer.tools ?? [], ungrounded = answer.ungrounded ?? [], cards = answer.cards ?? [], replies = answer.replies ?? [];
  // A fix question passes on a card that is one of suggest-fixes' fixes in this thread OR whose own engine comparison clears a violation and adds none (`fixes`).
  const searched = searchedFixes(replies);
  const good = (answer.fixes ?? 0) + cards.filter((c) => isSearched(c, searched) && !clearsCleanly(c.comparison)).length;
  const text = answer.text.replace(/\bNO GO(?![\w-])/g, "NO-GO"); // the app reads "NO GO" as NO-GO
  const unmarked = (q.marked ?? []).flatMap((p) => [...text.matchAll(new RegExp(p, "gi"))])
    .filter((m) => !(/(?<![\w-])GO$/.test(m[0]) && !/\ba GO$/i.test(m[0]) && sameSentenceAsVerdict(text, m.index, answer.verdict)))
    .filter((m) => !NEGATED.test(text.slice(0, m.index)) && !questioned(text, m.index) && !ungrounded.some((u) => m[0].toLowerCase().includes(u.toLowerCase())));
  const named = q.atLeast ? q.atLeast.of.filter((n) => word(n).test(answer.text)) : [];
  const why = [
    ...(q.all ?? []).filter((p) => !new RegExp(p, "i").test(answer.text)).map((p) => `missing /${p}/`),
    ...(q.none ?? []).filter((p) => new RegExp(p, "i").test(answer.text)).map((p) => `forbidden /${p}/`),
    ...(answer.proposals < (q.proposals ?? 0) ? [`wanted ${q.proposals} proposal(s), got ${answer.proposals}`] : []),
    ...(q.proposalsMax !== undefined && answer.proposals > q.proposalsMax ? [`wanted at most ${q.proposalsMax} proposal(s), got ${answer.proposals}`] : []),
    ...(good < (q.fixes ?? 0) ? [`wanted ${q.fixes} card(s) that are a searched fix or clear a violation and add none, got ${good}`, ...(answer.uncompared ? ["card has no engine comparison"] : [])] : []),
    ...(q.tools ?? []).filter((p) => !tools.some((t) => new RegExp(p, "i").test(t))).map((p) => `no tool /${p}/ called (called: ${tools.join(", ") || "none"})`),
    ...(q.verdict !== undefined && answer.verdict !== q.verdict ? [`verdict ${answer.verdict ?? "missing"}, wanted ${q.verdict}`] : []),
    ...unmarked.map((m) => `claim "${m[0]}" is not in ungrounded`),
    ...(q.ungroundedMax !== undefined && ungrounded.length > q.ungroundedMax ? [`ungrounded: ${ungrounded.join(", ")}`] : []),
    ...(q.atLeast && named.length < q.atLeast.n ? [`named ${named.length} of ${q.atLeast.of.length}, wanted ${q.atLeast.n}`] : []),
    ...(q.subject ? markdownIn(answer.text).map((m) => `Markdown in the answer: ${m}`) : []), // every harder question: the panel shows plain text
    ...cardProblems(q, cards, replies),
  ];
  const notes = [
    ...(q.datasheet ? cards.flatMap((c) => { const a = rec(c.args), named = weightNamed(String(a.source ?? "")); return rec(a.fields).physical_mass === undefined && named ? [`the part declares no mass: the number was named ${named}`] : []; }) : []),
    ...(q.searched && !replies.some((r) => r.command === "suggest-fixes") ? ["suggest-fixes was not called in this thread"] : []),
    ...(q.fixes && searched.length && good >= q.fixes && !cards.some((c) => isSearched(c, searched))
      ? cards.filter((c) => clearsCleanly(c.comparison)).map((c) => `passed on its own engine comparison; not one of the searched fixes: ${changeText(c)}`) : []),
    ...(q.cardDiff && q.cardAfter ? cards.map((c) => `card made by ${c.command === "build robot" ? "propose-robot" : c.command}`) : []), // the sizing question: propose-robot or a plain declare
  ];
  return { pass: why.length === 0, why, ...(notes.length ? { notes } : {}) };
}

/** A card's engine comparison (main/proposalCheck.ts) cleared at least one violation and added none: what an engine-searched fix guarantees. */
export const clearsCleanly = (comparison: unknown): boolean => {
  const changes = (comparison as { changes?: { kind: string; severity: string }[] } | undefined)?.changes;
  const any = (kind: string) => Array.isArray(changes) && changes.some((c) => c.kind === kind && c.severity === "violation");
  return any("cleared") && !any("new");
};

/** A card with no engine comparison at all (it timed out), not one whose comparison found nothing cleared. */
export const lacksComparison = (comparison: unknown): boolean => !Array.isArray((comparison as { changes?: unknown } | undefined)?.changes);

/** The cards a question counts: in a thread the cards so far plus this turn's, unless the question is `fresh` (this turn's only). */
export const countedCards = <T>(q: EvalQuestion, earlier: T[], turn: T[]): T[] => (q.thread && !q.fresh ? [...earlier, ...turn] : turn);

/** One line per subject ("concepts 4/4"), in SUBJECTS order, plus one for questions with no subject (the first 18). */
export function bySubject(results: { q: EvalQuestion; pass: boolean }[]): string[] {
  return [...SUBJECTS, undefined].flatMap((s) => {
    const of = results.filter((r) => r.q.subject === s);
    return of.length || s ? [`${s ?? "lookup (no subject)"} ${of.filter((r) => r.pass).length}/${of.length}`] : [];
  });
}
