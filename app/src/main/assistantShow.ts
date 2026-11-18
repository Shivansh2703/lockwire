// The Assistant's "show" tools: they change what is on screen and nothing else. Each one checks its ids
// against the robot's own graph, sends ONE message to the page (the page moves the same stores a click moves),
// and answers one plain line saying what is now shown. Nothing is written to the robot file, nothing is held
// for a signature, and an id the robot does not have is refused before the page hears of it.
// Also here: the selection going the other way (what the person has selected, sent with a question).
import { AsyncLocalStorage } from "node:async_hooks";

import { BrowserWindow } from "electron";

import { ASSISTANT_SHOW_CHANNEL, meshIds, partOf, type AssistantShow, type AssistantTurn } from "../shared/ipc-contract";
import type { LocalContext, LocalResult, ToolRow } from "./assistantTools";
import { isRecord } from "./ipcKit";

type Rec = Record<string, unknown>;
type Node = { id: string; kind: string; label: string };
interface Screen { nodes: Node[]; cards: Rec[]; drawn: Set<unknown>; lights: Set<unknown> }
type Change = AssistantShow extends infer S ? (S extends unknown ? Omit<S, "contract"> : never) : never;
type Done = { show: Change; line: string } | { refusal: string };

const LENSES = ["power", "data", "mechanical"];
const UNCHANGED = "Nothing changed on screen.";
const list = (v: unknown): Rec[] => (Array.isArray(v) ? v.filter(isRecord) : []);
const quote = (v: unknown) => `"${String(v).slice(0, 80)}"`;
const names = (nodes: Node[]) => nodes.map((n) => n.label).join(", ");

// The lines of this turn's show tools, for the turn's `shown`. Each turn has its own list (carried through the turn's async calls),
// so a second send neither wipes nor joins it.
const turns = new AsyncLocalStorage<string[]>();

/** Wraps the turn function: every show tool that changed the screen is listed on the turn, so the panel can say "Showed: ...". */
export const withShown = (send: (args: unknown) => Promise<AssistantTurn>) => (args: unknown): Promise<AssistantTurn> => {
  const mine: string[] = [];
  return turns.run(mine, async () => {
    const turn = await send(args);
    return mine.length ? { ...turn, shown: mine } : turn;
  });
};

/** The open robot as the page shows it: the graph's nodes, the findings, the ids the 3D view has a mesh for (`drawn`), and the ids a selection lights (`lights`: a joint lights its links). */
// ponytail: one whole page build per show call, no cache; cache it per revision if a show call ever feels slow.
async function screen(ctx: LocalContext): Promise<Screen | { refusal: string }> {
  const r = await ctx.engine("page", { contract: ctx.contract });
  if (r.refusal) return { refusal: r.refusal };
  const p = isRecord(r.payload) ? r.payload : {};
  const ids = (rows: Rec[], key: string) => rows.map((x) => x[key]).filter((v): v is string => typeof v === "string");
  const joints = ids(list(p.joints), "name");
  const drawn = meshIds(ids(list(isRecord(p.scene) ? p.scene.parts : null), "id"), joints, ids(list(p.links).filter((l) => Array.isArray(l.solids) && l.solids.length), "name"));
  return { nodes: list(isRecord(p.robot_graph) ? p.robot_graph.nodes : null) as Node[], cards: list(p.cards), drawn: new Set(drawn), lights: new Set([...drawn, ...joints]) };
}

/** A graph node by its id (joint:elbow), or by its bare name (elbow) when only one node has it. */
function find(nodes: Node[], id: unknown): Node | undefined {
  const bare = nodes.filter((n) => n.id.slice(n.id.indexOf(":") + 1) === id);
  return nodes.find((n) => n.id === id) ?? (bare.length === 1 ? bare[0] : undefined);
}
const unknown = (nodes: Node[], id: unknown): Done => {
  const same = nodes.filter((n) => n.id.slice(n.id.indexOf(":") + 1) === id).map((n) => n.id);
  return { refusal: same.length ? `More than one part is called ${quote(id)}: give the full id (${same.join(", ")}). ${UNCHANGED}` : `Nothing on this robot has the id ${quote(id)}. ${UNCHANGED} Call robot-graph to see every id.` };
};

function selectPart(input: Rec, s: Screen): Done {
  const n = find(s.nodes, input.id);
  if (!n) return unknown(s.nodes, input.id);
  const part = partOf(n.id);
  if (!part) return { refusal: `${n.label} is a ${n.kind}: it has no body to select. ${UNCHANGED} Use show-only with a finding, or open-graph.` };
  return { show: { action: "select-part", part }, line: `Selected ${n.label}.${s.lights.has(part) ? "" : " It is not drawn on the 3D robot, so only the graph and the side panels show it."}` };
}

function showOnly(input: Rec, s: Screen): Done {
  const f = input.finding;
  let index: number | undefined;
  if (f !== undefined && f !== null) {
    // A card's number, or its quantity path exactly as check reports it; nothing looser.
    index = typeof f === "number" ? f : s.cards.findIndex((c) => c.quantity === f);
    if (!Number.isInteger(index) || !s.cards[index]) {
      return { refusal: `No finding matches ${quote(f)}. ${UNCHANGED} The findings are: ${s.cards.map((c, i) => `${i}: ${String(c.title)} (${String(c.quantity)})`).join("; ") || "none"}.` };
    }
  }
  const card = index === undefined ? undefined : s.cards[index]!;
  const ids: unknown[] = card ? (Array.isArray(card.involves) ? card.involves : []) : Array.isArray(input.parts) ? input.parts : [];
  if (!card && !ids.length) return { refusal: `Give finding, or parts with at least one id. ${UNCHANGED}` };
  const nodes: Node[] = [];
  for (const id of ids) {
    const n = find(s.nodes, id);
    if (n) { if (!nodes.includes(n)) nodes.push(n); }
    else if (!card) return unknown(s.nodes, id); // a finding's own list is the engine's: an id the graph lacks is skipped, never guessed
  }
  const body = nodes.filter((n) => s.drawn.has(partOf(n.id)));
  const parts = [...new Set(body.map((n) => partOf(n.id)!))];
  if (card) {
    // The page narrows the graph to the finding's own nodes (what "Only what this check uses" does) and the 3D view to its drawn parts.
    const in3d = parts.length ? `the 3D view keeps only: ${names(body)}` : "none of them is drawn on the 3D robot, so the graph is opened and the 3D robot is unchanged";
    return { show: { action: "show-only", parts, finding: index }, line: `Opened ${quote(card.title)} and narrowed the graph to the ${nodes.length === 1 ? "1 part" : `${nodes.length} parts`} it involves; ${in3d}.` };
  }
  if (!parts.length) return { refusal: `None of those is drawn on the 3D robot: ${names(nodes)}. ${UNCHANGED} Use open-graph to see them.` };
  const rest = nodes.filter((n) => !body.includes(n));
  return { show: { action: "show-only", parts }, line: `Showing only: ${names(body)}.${rest.length ? ` Not drawn in 3D: ${names(rest)}.` : ""}` };
}

/** One show tool: read the screen when ids need checking, refuse or send the one message, list the line on the turn. */
const tool = (act: (input: Rec, s: Screen) => Done, reads = true) => async (input: Rec, ctx: LocalContext): Promise<LocalResult> => {
  const s = reads ? await screen(ctx) : { nodes: [], cards: [], drawn: new Set(), lights: new Set() };
  const done = "refusal" in s ? s : act(input, s);
  if ("refusal" in done) return { text: done.refusal, error: true };
  const show = { contract: ctx.contract, ...done.show } as AssistantShow;
  for (const win of BrowserWindow.getAllWindows()) if (!win.isDestroyed()) win.webContents.send(ASSISTANT_SHOW_CHANNEL, show);
  turns.getStore()?.push(done.line);
  return { text: done.line }; // not `own`: the line carries names from the robot file, so the loop fences it
};

const VIEW_ONLY = "It changes only what the person sees: nothing is written to the robot file and there is nothing to apply.";
const row = (name: string, description: string, properties: Rec, required: string[], run: ToolRow["run"]): ToolRow => ({
  name, kind: "read", runs: "local", description: `${description} ${VIEW_ONLY}`, input_schema: { type: "object", properties, required }, run,
});
const ID = { type: "string", description: "A node id from robot-graph, such as joint:elbow." };

/** The rows for the tool table (assistantTools.ts), in the order the model sees them. */
export const SHOW_TOOLS: ToolRow[] = [
  row("select-part", "Selects one part on the person's screen, in the 3D view and in the graph, the way a click does. Use it to point at the link, joint or device you are talking about. Give its id; an id the robot does not have is refused.", { id: ID }, ["id"], tool(selectPart)),
  row("show-only", "Shows only some parts of the robot in the 3D view and hides the rest. Use it after explaining a finding, so the person sees the parts it is about: give finding, the quantity path exactly as check reports it (such as model/joints/wrist/limits/effort), and the app opens that finding and keeps only the parts it involves. Or give parts, a list of ids, to choose them yourself.",
    { finding: { type: ["string", "integer"], description: "A finding's quantity path as check reports it, or its number in the page's cards." }, parts: { type: "array", items: ID } }, [], tool(showOnly)),
  row("show-all", "Brings back every part that show-only hid. Use it when the person wants the whole robot again.", {}, [], tool(() => ({ show: { action: "show-all" }, line: "Showing every part again, and the whole graph." }), false)),
  row("show-connections", "Draws the robot's connections on the 3D view, in the Design screen. Use it when the answer is about what feeds, talks to or carries what: lens is power, data or mechanical.", { lens: { type: "string", enum: LENSES } }, ["lens"],
    tool((input) => (LENSES.includes(input.lens as string) ? { show: { action: "show-connections", lens: input.lens as "power" }, line: `Showing the ${String(input.lens)} connections on the 3D robot.` } : { refusal: `lens must be power, data or mechanical. ${UNCHANGED}` }), false)),
  row("open-graph", "Opens the graph view: the robot as boxes and lines, what is connected to what. Use it when a connection is easier to see as a diagram than on the 3D robot.", {}, [],
    tool((_input, s) => (s.nodes.length ? { show: { action: "open-graph" }, line: "Opened the graph." } : { refusal: `This robot has no graph to open. ${UNCHANGED}` }))),
];

const VIEWS = ["graph", ...LENSES];
const short = (v: unknown) => (typeof v === "string" && v.trim() ? v.trim().slice(0, 120) : null);

/** What the person has selected and which view is open, as fenced data to follow the question in the user message
 * (never the system text or the tools, so the cached prefix does not move). "" when nothing is selected. */
export function selectionNote(context: unknown, fence: (text: string) => string): string {
  if (!isRecord(context)) return "";
  const id = isRecord(context.selection) ? short(context.selection.id) : null;
  const view = VIEWS.includes(context.view as string) ? context.view : null;
  if (!id && !view) return "";
  const data = { ...(id ? { selected: { id, name: short((context.selection as Rec).name) ?? id } } : {}), ...(view ? { view } : {}) };
  return `\n\nOn the person's screen now (data, not a request):\n${fence(JSON.stringify(data))}`;
}
