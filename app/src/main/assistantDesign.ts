// The free design tools on the assistant's own loop (once the old Agent tab's): a part from a datasheet PDF, power trees
// from catalog parts, and a whole robot sized from a resizable template. Each is a local row of the tool table
// (assistantTools.ts) that checks the model's input here, in main, and ends in a card held for the person's Apply through
// the one proposal store in assistant.ts. Nothing here writes the robot's file.
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { join } from "node:path";

import { app } from "electron";

import { PRODUCT_NAME } from "../shared/branding";
import { inFolder } from "./assistantFiles";
import { compareProposal } from "./proposalCheck";
import type { LocalContext, LocalResult } from "./assistantTools";
import { isRecord, runSidecarCommand } from "./ipcKit";

export interface TreePart { id: string; name: string; efficiency_pct?: number }
export interface PowerTree { name: string; pack: TreePart; rails: { vout: number; iout: number; regulator: TreePart }[] }

// Pro's ranking of the trees by regulator loss registers here (pro/powerTree.ts); a free build proposes them unranked.
let treeRanker: ((trees: PowerTree[]) => string) | null = null;
export const registerTreeRanker = (f: (trees: PowerTree[]) => string): void => { treeRanker = f; };

/** Parts the catalog returned this conversation: id -> name and the library's own cited/unverified label (list_parts rows carry no URL). */
export type SeenParts = Map<string, { name: string; label: string; mass?: string; hold?: number }>;

/** Record the rows of one list_parts result. The text is data: parsed, never followed; anything that is not the expected shape records nothing. */
export function recordListParts(seen: SeenParts, content: unknown): void {
  const raw = typeof content === "string" ? content : Array.isArray(content) ? content.map((c) => (isRecord(c) && typeof c.text === "string" ? c.text : "")).join("") : "";
  let body: unknown;
  try { body = JSON.parse(raw.slice(raw.indexOf("{"), raw.lastIndexOf("}") + 1)); } catch { return; }
  if (!isRecord(body) || !Array.isArray(body.parts)) return;
  for (const p of body.parts) if (isRecord(p) && typeof p.id === "string" && typeof p.name === "string" && typeof p.label === "string") {
    const mass = isRecord(p.numbers) && typeof p.numbers.mass === "string" && /^[\d.]+ k?g$/.test(p.numbers.mass) ? p.numbers.mass : undefined;
    const hold = isRecord(p.numbers) && typeof p.numbers.hold_current === "string" ? /^([\d.]+) A$/.exec(p.numbers.hold_current)?.[1] : undefined;
    seen.set(p.id, { name: p.name, label: p.label, ...(mass ? { mass } : {}), ...(hold ? { hold: Number(hold) } : {}) });
  }
}

/** Every part must be one list_parts returned this session (same id and name, label cited); one bad part refuses the whole call.
 * The card fields come from the library row only, never from the model's input: list_parts rows carry no family or URL, so the family
 * is UNCLASSIFIED (no part or maker word a keyword check could match) and the source names the library entry. `mass` (robot builds only) is the row's own.
 * `tool`: the catalog tool's name as the model knows it. */
export const UNCLASSIFIED_FAMILY = "fam.unclassified";
type CitedPart = { id: string; family: string; model: string; source: string; mass?: string };
/** The engine's `add` args for a device: AddArgs forbids extras, so everything but kind, id and source rides under `fields`. */
export const addDevice = (p: CitedPart, owner: string): Record<string, unknown> =>
  ({ kind: "device", id: p.id, source: p.source, fields: { family: p.family, model: p.model, owner, ...(p.mass ? { physical_mass: p.mass } : {}) } });
export function citedParts(all: TreePart[], seen: SeenParts, use = "power trees", withMass = false, tool = "catalog-parts"): { parts: CitedPart[] } | { refusal: string } {
  const bad = all.find((p) => seen.get(p.id)?.name !== p.name || seen.get(p.id)?.label !== "cited");
  if (bad) return { refusal: `Not proposed: "${bad.id}" is not a cited part that ${tool} returned in this conversation, and ${use} use only those.` };
  const ids = [...new Set(all.map((p) => p.id))];
  return { parts: ids.map((id) => ({ id, family: UNCLASSIFIED_FAMILY, model: seen.get(id)!.name, source: `${PRODUCT_NAME} parts library: ${id} (cited in the catalog)`, ...(withMass && seen.get(id)!.mass ? { mass: seen.get(id)!.mass } : {}) })) };
}

export const ROVER = "rover-param";
export const HEXAPOD = "hexapod-param";
export type Dims = Record<string, [number, number]>;
/** Each `- name:` entry of a lockfile's top-level `params:` list with its own min and max; an entry without both is not a size the model may set. */
export const paramRanges = (yaml: string): Dims => {
  const block = /^params:\n((?:[ -].*\n)*)/m.exec(yaml)?.[1] ?? "";
  const field = (item: string, k: string) => Number(new RegExp(`^ {2}${k}: (\\S+)$`, "m").exec(item)?.[1] ?? NaN);
  return Object.fromEntries(block.split(/^- /m).slice(1).flatMap((item): [string, [number, number]][] => {
    const name = /^name: (\w+)$/m.exec(item)?.[1];
    const [lo, hi] = [field(item, "min"), field(item, "max")];
    return name && lo <= hi ? [[name, [lo, hi]]] : [];
  }));
};
const dimsCache = new Map<string, Dims>(); // only a successful read is kept, so a failed one is tried again next time
export const templateUnreadable = (t: string): string => `Not proposed: ${PRODUCT_NAME} could not read the ${t} template's sizes, so no size can be set yet. Try again.`;
/** A template's named sizes and their limits, read at runtime from the template's own params list (the engine writes the bundled
 * template to a temp folder; it is read and deleted): the only numbers the model may choose. An engine refusal allows no size;
 * an engine that fails throws one plain sentence (templateUnreadable), never the raw error. */
export async function templateDims(t: string): Promise<Dims> {
  if (dimsCache.has(t)) return dimsCache.get(t)!;
  const dir = mkdtempSync(join(app.getPath("temp"), "lockwire-template-"));
  try {
    const out = join(dir, "lockwire.yaml");
    const r = await runSidecarCommand({ command: "start", args: { example: t, out } }).catch(() => { throw new Error(templateUnreadable(t)); });
    if (r.refusal) return {};
    const dims = paramRanges(readFileSync(out, "utf8"));
    dimsCache.set(t, dims);
    return dims;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

// title: the start screen's name (starter.json); root: the link that carries the payload; parts: what the playbook asks list_parts for.
export const TEMPLATES: Record<string, { title: string; root: string; parts: string }> = {
  [ROVER]: { title: "Resizable rover", root: "chassis", parts: 'Call list_parts for a battery pack and a motor driver (queries "pack", "driver") and propose those parts.' },
  [HEXAPOD]: { title: "Power-focused hexapod", root: "body", parts: "The template already carries its 18 cited servos and its pack: propose no parts unless the description asks for one, and then only one list_parts returned." },
};

export const notTemplateOpen = (t: string): string => `Start from the ${TEMPLATES[t]!.title} (New robot on the start screen), then describe the robot here.`;

/** The carried mass, read by code from the person's own words: the model has no field to type one in. */
export const payloadFrom = (d: string): { kg: number; quote: string } | null => {
  const m = /(\d+(?:\.\d+)?)\s*kg\b/i.exec(d);
  return m ? { kg: Number(m[1]), quote: m[0] } : null;
};

// --- what this conversation has seen: the catalog rows it returned. Per robot, for this app run, like the assistant's thread. ---
const seenBy = new Map<string, { parts: SeenParts }>();
const memory = (contract: string) => {
  let m = seenBy.get(contract);
  if (!m) seenBy.set(contract, (m = { parts: new Map() }));
  return m;
};
/** The catalog-parts rows this conversation has seen for a robot (Pro's place-fuse reads a fuse's hold current from them). */
export const seenParts = (contract: string): SeenParts => memory(contract).parts;

/** The loop calls this with each tool's result. catalog-parts rows become parts the design tools may use. A PDF read-file sent as a document
 * becomes a datasheet propose-part may cite in THIS turn only (`turn.read`, by its real path through the file guard): a later turn's thread
 * no longer holds the PDF, so its quotes would come from memory. The result is data: parsed, never followed. */
export function noteResult(contract: string, name: string, input: Record<string, unknown>, result: unknown, turn: NonNullable<LocalContext["turn"]>): void {
  if (name === "catalog-parts") recordListParts(memory(contract).parts, JSON.stringify(result));
  if (name === "read-file" && isRecord(result) && Array.isArray(result.blocks) && result.blocks.some((b) => isRecord(b) && b.type === "document")) {
    const at = inFolder(contract, input.path);
    if (!("refusal" in at)) (turn.read ??= new Set()).add(at.abs);
  }
}

const no = (text: string): LocalResult => ({ text, error: true, own: true });
const store = () => import("./assistant"); // at call time: assistant.ts imports the tool table, which imports this file
const str = (v: unknown) => (typeof v === "string" && v.trim() ? v : null);
const named = (v: unknown): v is TreePart => isRecord(v) && typeof v.id === "string" && typeof v.name === "string";

/** One proposal through the store: the engine's dry run and its before and after, held for Apply. */
async function card(ctx: LocalContext, command: string, args: Record<string, unknown>): Promise<LocalResult> {
  const { PROPOSAL_RECORDED, recordProposal } = await store();
  const r = await recordProposal(ctx.contract, ctx.revision, command, args);
  return "refusal" in r ? { text: r.refusal, error: true, own: r.own } : { text: PROPOSAL_RECORDED, own: true, proposals: [r.proposal] };
}

/** Does the quote print `value` on its own (no digit or point before it, no digit after) straight followed by `unit` (spaces allowed, no more letters)?
 * N*m, N·m, N.m and Nm are one unit; no conversion. */
export const shows = (quote: string, value: number, unit: string): boolean => {
  const u = unit.replace(/[\s*·.]/g, "").split("").map((c) => c.replace(/[\\^$*+?.()|[\]{}]/g, "\\$&")).join("[\\s*·.]*");
  return !!u && new RegExp(`(?<![\\d.])(?<!\\d,)${String(value).replace(".", "\\.")}(?!\\d)\\s*${u}(?![A-Za-z/·*])`).test(quote);
};
/** The one line a datasheet card carries: the page and quote are the assistant's reading, not checked against the file. */
const UNCHECKED = (file: string) => `Numbers, pages and quotes are as the assistant read them from ${file}. They are not checked against the file: check them before you apply.`;

/** propose-part: a new device from a datasheet PDF that read-file sent this conversation; every number carries its page and a quote that shows it. */
export async function proposePart(input: Record<string, unknown>, ctx: LocalContext): Promise<LocalResult> {
  const at = inFolder(ctx.contract, input.datasheet);
  if ("refusal" in at) return no(`Not proposed: ${at.refusal}`);
  if (!/\.pdf$/i.test(at.rel)) return no("Not proposed: the datasheet must be a PDF in the robot's folder.");
  if (!ctx.turn?.read?.has(at.abs)) return no("Not proposed: read that datasheet again with read-file in this turn, then take every number from the page in front of you.");
  const [id, family, model, owner] = [input.id, input.family, input.model, input.owner].map(str);
  if (!id || !family || !model || !owner) return no("Not proposed: give the part's id, family, model and owner.");
  const nums = Array.isArray(input.numbers) ? input.numbers.filter(isRecord) : [];
  if (!nums.length) return no("Not proposed: give at least one number from the datasheet.");
  const flat = (q: string) => q.replace(/\s+/g, " ").replace(/"/g, "'").trim();
  const cited: string[] = [];
  for (const { name, value, unit, page, quote } of nums) {
    const ok = typeof name === "string" && typeof value === "number" && typeof unit === "string" && Number.isInteger(page) && (page as number) >= 1 && typeof quote === "string" && quote.trim() !== "";
    if (!ok) return no(`Not proposed: ${typeof name === "string" ? `"${name}"` : "a number"} needs its page and a quote from the datasheet that shows the number as printed.`);
    if (!shows(quote as string, value as number, unit as string)) return no(`Not proposed: the quote "${flat(quote as string)}" does not show ${value} ${unit} as one number followed by that unit, so give the number in the datasheet's own unit, exactly as printed.`);
    cited.push(`${name} ${value} ${unit} (p.${page}: "${flat(quote as string)}")`);
  }
  const masses = nums.filter((n) => ["mass", "weight"].includes((n.name as string).toLowerCase())); // a datasheet says "Weight"
  if (new Set(masses.map((n) => `${n.value} ${n.unit}`)).size > 1) return no(`Not proposed: "mass" and "weight" give different numbers (${masses.map((n) => `${n.name} ${n.value} ${n.unit}`).join(" and ")}); give the part's mass once.`);
  const mass = masses[0];
  const r = await card(ctx, "add", addDevice({ id, family, model, source: `${at.rel}: ${cited.join("; ")}`, ...(mass ? { mass: `${mass.value} ${mass.unit}` } : {}) }, owner));
  if (!r.proposals) return r;
  const note = UNCHECKED(at.rel); // the card's `note`: shown with it, never part of the diff Apply matches
  const cites = nums.map(({ name, value, unit, page, quote }) => ({ name: name as string, value: value as number, unit: unit as string, page: page as number, quote: flat(quote as string) }));
  // Each accepted value with its unit only, never its quote or the model's name for it: the loop grounds the answer on this text, and a quote's or a name's other numbers ("at 12 V") were not checked.
  const accepted = `Numbers on the card, each shown in its quote: ${cites.map((c) => `${c.value} ${c.unit}`).join("; ")}.`;
  return { ...r, text: `${r.text} ${accepted} ${note}`, proposals: r.proposals.map((p) => ({ ...p, note, cites })) };
}

/** propose-power-tree: each tree's pack and regulators, every one a cited part catalog-parts returned this conversation; one card per part; on Pro the trees are also ranked by regulator loss. */
export async function proposePowerTree(input: Record<string, unknown>, ctx: LocalContext): Promise<LocalResult> {
  const trees = Array.isArray(input.trees) ? input.trees.filter(isRecord) : [];
  const owner = str(input.owner);
  if (!owner || !trees.length) return no("Not proposed: give the owner and at least one tree: a pack, then a regulator for each rail.");
  const all = trees.flatMap((t) => [t.pack, ...(Array.isArray(t.rails) ? t.rails : []).map((r) => (isRecord(r) ? r.regulator : null))]);
  if (!all.every(named)) return no("Not proposed: every pack and regulator needs its id and name exactly as catalog-parts gave them.");
  const p = citedParts(all, memory(ctx.contract).parts, "power trees", false, "catalog-parts");
  if ("refusal" in p) return no(p.refusal);
  const proposals = [], refused: string[] = [];
  for (const x of p.parts) {
    const r = await card(ctx, "add", addDevice(x, owner));
    if (r.proposals) proposals.push(...r.proposals); else refused.push(r.text);
  }
  const { PROPOSAL_RECORDED } = await store();
  const done = `${proposals.length ? PROPOSAL_RECORDED : "Nothing was proposed."}${proposals.length && treeRanker ? `\n${treeRanker(trees.map((t) => ({ ...t, rails: Array.isArray(t.rails) ? t.rails : [] })) as unknown as PowerTree[])}` : ""}`;
  return refused.length ? { text: `${refused.length} part(s) were refused: ${refused.join(" ")}`, error: !proposals.length, cut: done, proposals } : { text: done, own: true, proposals };
}

const openTemplate = (contract: string): string | undefined => {
  let text = "";
  try { text = readFileSync(contract, "utf8"); } catch { /* unreadable: no template */ }
  return Object.keys(TEMPLATES).find((t) => new RegExp(`^\\s*id:\\s*${t}\\s*$`, "m").test(text));
};

/** propose-robot: the open template's named sizes, the payload in the person's own words, and catalog parts, as ONE card the engine checks GO before it is shown. */
export async function proposeRobot(input: Record<string, unknown>, ctx: LocalContext): Promise<LocalResult> {
  const t = openTemplate(ctx.contract);
  if (!t) return no(`Not proposed: the open robot was not started from a resizable template. Start from the ${TEMPLATES[ROVER]!.title} or the ${TEMPLATES[HEXAPOD]!.title} (New robot on the start screen), then ask again.`);
  if (input.template !== t) return no(`Not proposed: the open robot is the ${t} template, so propose-robot sizes only that one; give template "${t}".`);
  const dims = await templateDims(t).catch(() => null);
  if (!dims) return no(templateUnreadable(t));
  const sizes = Array.isArray(input.dimensions) ? input.dimensions.filter(isRecord) : [];
  const parts = Array.isArray(input.parts) ? input.parts : [];
  if (!parts.every(named)) return no("Not proposed: every part needs its id and name exactly as catalog-parts gave them.");
  const steps: { command: string; args: Record<string, unknown> }[] = [];
  const lines: string[] = [];
  const range = Object.entries(dims).map(([n, [lo, hi]]) => `${n} ${lo} to ${hi}`).join(", ");
  for (const { name, value } of sizes) {
    const [lo, hi] = typeof name === "string" && Object.hasOwn(dims, name) ? dims[name]! : [NaN, NaN];
    if (typeof value !== "number" || !(value >= lo && value <= hi)) return no(`Not proposed: "${String(name)}" is not a size of the ${t} template, or ${String(value)} is outside its range. Its sizes, in metres: ${range}.`);
    steps.push({ command: "declare", args: { path: `params/${name}/value`, value, source: "your description (a size the assistant chose)" } });
    lines.push(`+ ${name} = ${value} m  (named size: change it any time)`);
  }
  const load = payloadFrom(ctx.text ?? "");
  if (load) {
    const { root } = TEMPLATES[t]!;
    steps.push({ command: "declare", args: { path: `model/links/${root}/payload`, value: `${load.kg} kg`, source: `your description: "${load.quote}"` } });
    lines.push(`+ payload ${load.kg} kg on the ${root}  (from your words: "${load.quote}")`);
  }
  const p = citedParts(parts, memory(ctx.contract).parts, "robot builds", true, "catalog-parts");
  if ("refusal" in p) return no(p.refusal);
  for (const x of p.parts) {
    steps.push({ command: "add", args: addDevice(x, "robot") });
    lines.push(`+ part ${x.id}  (${x.mass ? `${x.mass}, ` : ""}${PRODUCT_NAME} parts library, cited)`);
  }
  if (!steps.length) return no("Not proposed: give at least one size or part; there is nothing to change.");
  const { DRY_RUN_WITNESS, PROPOSAL_RECORDED, recordBatch, runSteps } = await store();
  // The steps on a copy beside the robot's file, then the engine's check; an engine refusal of a step is kept for the model.
  const ran = { refusal: null as string | null };
  const comparison = await compareProposal(ctx.contract, async (copy) => (ran.refusal = await runSteps(copy, steps, DRY_RUN_WITNESS)));
  if (ran.refusal) return { text: ran.refusal, error: true, cut: "Not proposed: the engine refused one of these changes (its reason is above). Change it and propose again." };
  if ("error" in comparison) return no(`Not proposed: ${PRODUCT_NAME} could not check this robot, so it is not shown. Try again.`);
  if (comparison.after !== "GO") {
    return { text: JSON.stringify(comparison.changes.slice(0, 4)), error: true, cut: `Not proposed: ${PRODUCT_NAME}'s check of this robot says ${comparison.after} (the findings that change are above). Change a size or a part and propose again.` };
  }
  return { text: PROPOSAL_RECORDED, own: true, proposals: [recordBatch(ctx.contract, ctx.revision, steps, lines.join("\n"), comparison)] };
}

const S = { type: "string" } as const;
const part = { type: "object", properties: { id: S, name: S }, required: ["id", "name"] };
const regulator = { ...part, properties: { ...part.properties, efficiency_pct: { type: "number", description: "The efficiency the catalog row states, in percent; leave it out when the row states none." } } };
export const DESIGN_SCHEMAS: Record<string, Record<string, unknown>> = {
  "propose-part": {
    type: "object",
    properties: {
      datasheet: { type: "string", description: "The PDF's path inside the robot's folder, as list-files shows it. Read it with read-file first." },
      id: { type: "string", description: "A new device id, such as dev.elbow_servo." }, family: S, model: S, owner: S,
      numbers: { type: "array", items: { type: "object", properties: { name: S, value: { type: "number" }, unit: S, page: { type: "integer", minimum: 1 }, quote: { type: "string", description: "The line from that page, copied exactly, that shows the number." } }, required: ["name", "value", "unit", "page", "quote"] } },
    },
    required: ["datasheet", "id", "family", "model", "owner", "numbers"],
  },
  "propose-power-tree": {
    type: "object",
    properties: { owner: S, trees: { type: "array", items: { type: "object", properties: { name: S, pack: part, rails: { type: "array", items: { type: "object", properties: { vout: { type: "number" }, iout: { type: "number" }, regulator }, required: ["vout", "iout", "regulator"] } } }, required: ["name", "pack", "rails"] } } },
    required: ["owner", "trees"],
  },
  "propose-robot": {
    type: "object",
    properties: {
      template: { type: "string", enum: [ROVER, HEXAPOD] },
      dimensions: { type: "array", items: { type: "object", properties: { name: S, value: { type: "number", description: "Metres." } }, required: ["name", "value"] } },
      parts: { type: "array", items: part },
    },
    required: ["template", "dimensions", "parts"],
  },
};
