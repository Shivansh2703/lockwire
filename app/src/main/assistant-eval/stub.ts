// The keyless dry run's model (LOCKWIRE_EVAL_STUB=1, live.eval.ts): it replaces fetch for the whole run. The provider's URL gets scripted
// replies (real tool calls for the questions in SCRIPTED, "stub answer" for the rest); the local engine (loopback) passes; every other host
// throws. No key is read or sent. The scripted questions must pass through the real tools and engine; the others fail on their text, as they must.
type Step = string | [string, Record<string, unknown>];
type Msg = { role: string; content: unknown };

const FUSE = "topology/wires/w.pack/fuse", WRIST = "model/joints/wrist/limits/effort";
/** Scripted conversations, by the person's question; "What is the smallest fix?" on the servo bench is told apart by the robot path in the system text. */
const SCRIPTS: Record<string, Step[]> = {
  "Add this servo from its datasheet.": [
    ["read-file", { path: "lw-t1-datasheet.pdf" }],
    ["propose-part", { datasheet: "lw-t1-datasheet.pdf", id: "dev.lw-t1", family: "fam.servo.test", model: "LW-T1", owner: "you", numbers: [
      { name: "mass", value: 57, unit: "g", page: 1, quote: "Weight 57 g" },
      { name: "stall_torque", value: 0.52, unit: "N*m", page: 1, quote: "Stall torque 0.52 N*m at 6 V" },
      { name: "stall_current", value: 1.2, unit: "A", page: 1, quote: "Stall current 1.2 A at 6 V" }] }],
    "Here is the card for the LW-T1: mass 57 g, stall torque 0.52 N*m at 6 V and stall current 1.2 A at 6 V, each with page 1 and its line. Apply it to add the part.",
  ],
  "Add this servo from its datasheet, with its continuous torque too.": [
    ["read-file", { path: "lw-t1-datasheet.pdf" }],
    "The datasheet prints no continuous torque, only a stall torque of 0.52 N*m at 6 V, so I proposed nothing.",
  ],
  "Pick a battery pack and a 5 V regulator for this robot from the catalog and propose them.": [
    ["catalog-parts", { query: "pack" }],
    ["catalog-parts", { query: "buck" }],
    ["propose-power-tree", { owner: "power", trees: [{ name: "3S pack", pack: { id: "pack.gensace.3s-5000-60c-short", name: "Gens ace 5000mAh 3S 60C short-size (XT60)" }, rails: [{ vout: 5, iout: 5, regulator: { id: "pololu.d24v50f5", name: "Pololu 5V 5A step-down regulator D24V50F5" } }] }] }],
    "Two cards: a Gens ace 3S 5000 mAh pack and a Pololu D24V50F5 regulator, 5 V at 5 A. Apply the ones you want.",
  ],
  "Pick a battery pack and a 5 V regulator for this robot from the catalog and propose them; use the Lockwire test pack LW-P9 as the pack.": [
    ["catalog-parts", { query: "LW-P9" }],
    "There is no LW-P9 in the catalog, so I cannot use it. Pick a cited pack or give me its datasheet.",
  ],
  "Size this rover from its template with a chassis 0.6 m long.": [
    ["propose-robot", { template: "rover-param", dimensions: [{ name: "chassis_length", value: 0.6 }], parts: [] }],
    "One card: chassis_length 0.6 m, which Lockwire checks GO. Apply it to resize.",
  ],
  "And the fuse?": [
    ["suggest-fixes", { finding: FUSE }],
    ["declare", { path: "topology/rails/rail.battery/current_limit", value: "7 A", source: "suggest-fixes: a fuse at the wire's ampacity" }],
    "Fit a 7 A fuse on the battery rail, what the 22 AWG pack wire can carry. Apply the card to write it; nothing is written yet.",
  ],
  "Does the fuse fix change anything else?": [
    "Rows scored against the battery rail's fuse keep less headroom at 7 A. Current limits, wire ampacity and power budgets were not compared, so re-run the check after you apply it.",
  ],
};
const BENCH_FIX: Step[] = [
  ["suggest-fixes", { finding: WRIST }],
  ["swap", { joint: "wrist", part_id: "dynamixel.xl330-m288" }],
  "Swap the wrist servo for the XL330-M288; apply the card to write it. Nothing is written yet.",
];
/** The ids the stub run must pass (live.eval.ts fails the stub run if one of these fails). */
export const SCRIPTED = ["servo-bench-datasheet-part", "servo-bench-datasheet-gap", "rover-power-tree", "rover-power-tree-unknown-pack", "rover-param-resize", "armpower-fuse", "armpower-fuse-else", "servo-bench-fix"];

const LOCAL = new Set(["127.0.0.1", "localhost", "[::1]"]);
export function installStub(log: (line: string) => void): void {
  const real = globalThis.fetch;
  let n = 0;
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    const u = new URL(url instanceof Request ? url.url : String(url));
    if (LOCAL.has(u.hostname)) return real(url, init);
    if (u.href !== "https://api.anthropic.com/v1/messages") throw new Error(`eval stub: refused ${u.href}`);
    const body = JSON.parse(String(init?.body)) as { messages: Msg[]; system?: unknown };
    const start = body.messages.findLastIndex((m) => m.role === "user" && typeof m.content === "string");
    const question = String(body.messages[start]?.content ?? "").split("\n")[0]!;
    const results = body.messages.slice(start + 1).filter((m) => m.role === "user");
    const last = results.at(-1)?.content;
    if (Array.isArray(last)) {
      const doc = (last as { content?: unknown }[]).flatMap((r) => (Array.isArray(r.content) ? (r.content as { type?: string; source?: { media_type?: string } }[]) : [])).find((b) => b.type === "document");
      if (doc) log(`STUB: "${question}": read-file sent a document block (${doc.source?.media_type})`);
    }
    const bench = question === "What is the smallest fix?" && JSON.stringify(body.system ?? "").includes("servo-bench-");
    const step = ((bench ? BENCH_FIX : SCRIPTS[question]) ?? ["stub answer"])[results.length] ?? "stub answer";
    const content = typeof step === "string" ? [{ type: "text", text: step }] : [{ type: "tool_use", id: `stub${n++}`, name: step[0], input: step[1] }];
    return new Response(JSON.stringify({ content, stop_reason: typeof step === "string" ? "end_turn" : "tool_use", usage: { input_tokens: 0, output_tokens: 0 } }), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
}
