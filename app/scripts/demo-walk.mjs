// Demo pre-flight: walks every beat of the demo script in the real app, hidden, against
// the real engine, with the model's replies scripted. Run: (cd app && npm run build && TMPDIR=<scratch> node scripts/demo-walk.mjs <out-dir>)
// It prints one line per step (PASS or FAIL), the fix-search timings, and writes one picture per step into <out-dir>.
//
// Same launch as assistant-shots.mjs (e2e-drive's launchApp: the app's own hidden-launch gate; fresh user-data folder; a copy
// of each robot under the temp folder; a made-up key the keychain stand-in "saves"). The model stand-in differs in one way:
// each request to the Anthropic address is HELD in the main process until this script answers it, so every scripted reply is
// written from the real engine's tool results of the request before it (the real tool calls feed the next call). No paid
// call, no key, no switch or test code in the app. The start screen is never captured.
import { copyFileSync, cpSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { launchApp, stubDialogs, waitForSidecar } from "./e2e-drive.mjs";

const APP = join(dirname(fileURLToPath(import.meta.url)), "..");
const REPO = join(APP, "..");
const outDir = process.argv[2];
if (!outDir) throw new Error("usage: node scripts/demo-walk.mjs <out-dir>");
mkdirSync(outDir, { recursive: true });
const ONLY = process.env.DEMO_WALK_ONLY?.split(","); // e.g. DEMO_WALK_ONLY=b2,b4 runs only those beats
const log = (...a) => { if (process.env.DEMO_WALK_DEBUG) console.log(new Date().toISOString().slice(11, 19), ...a); };
const settle = (ms) => new Promise((r) => setTimeout(r, ms));

const use = (name, input) => ({ content: [{ type: "tool_use", id: `t-${name}-${Math.random().toString(36).slice(2, 8)}`, name, input }], stop_reason: "tool_use" });
const say = (text) => ({ content: [{ type: "text", text }], stop_reason: "end_turn" });
/** The tool results the app sent in its latest request: [{ name, text, error }], name from the tool_use it answers. */
function resultsOf(body) {
  const uses = new Map();
  for (const m of body.messages) if (m.role === "assistant" && Array.isArray(m.content)) for (const c of m.content) if (c.type === "tool_use") uses.set(c.id, c.name);
  const last = body.messages.at(-1);
  if (last?.role !== "user" || !Array.isArray(last.content)) return [];
  return last.content.filter((c) => c.type === "tool_result").map((c) => ({
    name: uses.get(c.tool_use_id),
    error: !!c.is_error,
    text: typeof c.content === "string" ? c.content : (c.content ?? []).map((b) => b.text ?? `[${b.type}]`).join("\n"),
    blocks: Array.isArray(c.content) ? c.content.map((b) => b.type) : ["text"],
  }));
}
/** A tool result's JSON, with the app's untrusted-text markers taken off. */
const json = (r) => { const t = r.text.replace(/^<<<UNTRUSTED[^\n]*\n/, "").replace(/\n?UNTRUSTED_CONTRACT_TEXT-\w+>>>\s*$/, ""); try { return JSON.parse(t); } catch { return null; } };

const results = []; // { beat, step, tools, screen, pass, why }
const timings = []; // { beat, tool, ms }
const dumps = []; // every tool result the app sent the model: { beat, name, error, text }
// No machine paths: every request to the model, in every beat, is read for an absolute path (a PDF's bytes are left out: base64 is not text).
const ABSOLUTE = /\/(?:Users|private|tmp|var)\/|(?<!\w)[A-Za-z]:\\\\/; // a drive letter stands alone: "instance:" before an escaped newline is not C:\
const leaks = []; // { beat, at }
const noBytes = (k, v) => (k === "data" && typeof v === "string" ? "[bytes]" : v);

async function launch(robot, theme, extraEnv = {}) {
  const tmp = mkdtempSync(join(tmpdir(), "lw-demo-walk-"));
  writeFileSync(join(tmp, "config.toml"), "");
  const { app, page } = await launchApp({
    args: [".", "--user-data-dir=" + join(tmp, "ud")], cwd: APP,
    env: { LOCKWIRE_WITNESS_CONFIG: join(tmp, "config.toml"), LOCKWIRE_SIDECAR_COMMAND: "uv", LOCKWIRE_SIDECAR_ARGS: "run --project " + REPO + " lockwire serve --exit-on-stdin-close", ...extraEnv },
  });
  if (await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().some((w) => w.isVisible()))) { await app.close(); throw new Error("a window is visible: stopped"); }
  const js = (s) => page.evaluate(s);
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e).slice(0, 300)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text().slice(0, 600)); });
  const until = async (what, cond, ms = 60_000) => {
    for (const t0 = Date.now(); Date.now() - t0 < ms; await settle(150)) if (await js(cond)) return;
    throw new Error(`timed out waiting for ${what}. The page reads: ${(await js(`document.body.textContent`)).slice(-600)}. Page errors: ${errors.join(" | ") || "none"}`);
  };
  const click = (name, scope = "document") => js(`(() => { const b = Array.from(${scope}.querySelectorAll("button")).find((b) => b.textContent.trim() === ${JSON.stringify(name)} && !b.disabled); b?.click(); return !!b; })()`);
  const shot = async (name) => {
    await settle(700);
    if (await js(`!!document.querySelector(".open-face")`)) throw new Error("the start screen is showing: not captured");
    if (await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().some((w) => w.isVisible()))) throw new Error("a window became visible: stopped");
    const bytes = Buffer.from(await app.evaluate(async ({ BrowserWindow }) => Array.from((await BrowserWindow.getAllWindows()[0].webContents.capturePage()).toPNG())));
    const file = `${name}-${theme}.png`;
    writeFileSync(join(outDir, file), bytes);
    return file;
  };
  await app.evaluate(({ BrowserWindow, safeStorage }) => {
    BrowserWindow.getAllWindows()[0].setContentSize(1280, 800);
    safeStorage.isEncryptionAvailable = () => true;
    safeStorage.encryptString = (s) => Buffer.from(`walk:${s}`);
    safeStorage.decryptString = (b) => b.toString().slice(5);
    const real = globalThis.fetch;
    globalThis.__lwHeld = [];
    globalThis.fetch = async (url, init) => {
      if (String(url) !== "https://api.anthropic.com/v1/messages") return real(url, init);
      return new Promise((resolve) => globalThis.__lwHeld.push({ at: Date.now(), body: String(init?.body ?? "{}"), answer: (reply) => resolve(new Response(JSON.stringify(reply), { status: 200, headers: { "content-type": "application/json" } })) }));
    };
  });
  log("launched");
  await stubDialogs(app, { robot });
  await js(`localStorage.setItem("lockwire-theme", "${theme}")`);
  await page.reload(); await page.waitForLoadState("load"); await waitForSidecar(page);
  await until("Open robot", `!!Array.from(document.querySelectorAll("button")).find((b) => b.textContent === "Open robot")`);
  log("sidecar ready");
  await click("Open robot");
  await until("the check", `!!document.querySelector(".check-headline:not(.verdict-pending)")`, 120_000);
  await settle(2500); // the 3D view settles

  const answers = () => js(`document.querySelectorAll('.assistant-turn[data-who="assistant"]').length`);
  /** One question. `steps` are functions of (tool results of the request, request body) returning the model's next reply. */
  const ask = async (beat, text, steps) => {
    const before = await answers();
    const calls = [];
    let sentAt = 0, sentTools = [];
    await page.fill('[aria-label="Ask about this robot"]', text);
    await page.press('[aria-label="Ask about this robot"]', "Enter");
    for (let i = 0; ; ) {
      const held = await app.evaluate(() => globalThis.__lwHeld.length ? { body: globalThis.__lwHeld[0].body, at: globalThis.__lwHeld[0].at } : null);
      if (held) {
        const body = JSON.parse(held.body);
        const sent = JSON.stringify({ system: body.system, messages: body.messages }, noBytes), leak = sent.match(ABSOLUTE);
        if (leak) leaks.push({ beat, at: sent.slice(Math.max(0, leak.index - 80), leak.index + 80) });
        const res = resultsOf(body);
        dumps.push(...res.map((r) => ({ beat, name: r.name, error: r.error, blocks: r.blocks, text: r.text })));
        if (sentTools.length) for (const t of sentTools) timings.push({ beat, tool: t, ms: held.at - sentAt });
        if (i >= steps.length) throw new Error(`"${text}": the app asked the model ${i + 1} times, the script has ${steps.length} replies`);
        log("model asked", i, res.map((r) => `${r.name}${r.error ? "!" : ""}:${r.text.slice(0, 120)}`));
        const reply = await steps[i](res, body);
        i += 1;
        sentTools = reply.content.filter((c) => c.type === "tool_use").map((c) => (c.input?.finding ? `${c.name} (${c.input.finding.split("/").slice(-2).join("/")})` : c.name));
        for (const c of reply.content) if (c.type === "tool_use") calls.push(`${c.name}(${JSON.stringify(c.input)})`);
        sentAt = await app.evaluate((_, r) => { const h = globalThis.__lwHeld.shift(); const t = Date.now(); h.answer(r); return t; }, reply);
        if (reply.stop_reason === "end_turn") {
          await until(`the answer to "${text}"`, `document.querySelectorAll('.assistant-turn[data-who="assistant"]').length === ${before + 1}`);
          if (i !== steps.length) throw new Error(`"${text}": ${steps.length - i} scripted replies were never asked for`);
          return calls;
        }
      }
      await settle(50);
    }
  };
  const allow = async () => {
    await click("Assistant");
    await until("the allow screen", `!!document.querySelector('.shell-drawer input[type="password"]')`);
    await page.fill('.shell-drawer input[type="password"]', "sk-ant-made-up-for-the-walk");
    await click("Save key and allow for this robot");
    await until("the question box", `!!document.querySelector('[aria-label="Ask about this robot"]')`);
  };
  const drawer = () => js(`document.querySelector(".shell-drawer")?.innerText ?? ""`);
  const close = () => app.close().catch(() => {});
  return { app, page, js, until, click, shot, ask, allow, drawer, close, errors, tmp };
}

/** A copy of one of our shipped robots under the temp folder. Returns the contract path. */
function copyRobot(name) {
  const tmp = mkdtempSync(join(tmpdir(), "lw-demo-robot-"));
  const src = join(REPO, "src/lockwire/examples", name);
  if (name.endsWith(".yaml")) { mkdirSync(join(tmp, "servo-bench")); copyFileSync(src, join(tmp, "servo-bench", name)); return join(tmp, "servo-bench", name); }
  cpSync(src, join(tmp, name), { recursive: true });
  return join(tmp, name, "contract.yaml");
}

const record = (beat, step, tools, screen, pass, why = "") => {
  results.push({ beat, step, tools, screen, pass, why });
  console.log(`${pass ? "PASS" : "FAIL"} ${beat} ${step}: ${screen}${why ? ` | ${why}` : ""}${tools.length ? ` | tools: ${tools.join(", ")}` : ""}`);
};

// ---- what the screen shows ----
const verdictOf = (s) => s.js(`(() => { const h = document.querySelector(".check-headline"); return h ? (h.querySelector(".check-verdict-word")?.textContent ?? "") + " | " + (h.querySelector(".check-count")?.textContent ?? "") : "no verdict"; })()`);
const lastAnswer = (s) => s.js(`Array.from(document.querySelectorAll('.assistant-turn[data-who="assistant"]')).at(-1)?.innerText ?? ""`);
const unverified = (s) => s.js(`Array.from(document.querySelectorAll('.assistant-turn[data-who="assistant"]')).at(-1)?.querySelectorAll('.assistant-unverified[title="unverified"]').length ?? 0`);
/** The words of the newest answer marked as the robot after a proposed change, not now (review F1): reported, never a failure. */
const proposedMarks = (s) => s.js(`Array.from(Array.from(document.querySelectorAll('.assistant-turn[data-who="assistant"]')).at(-1)?.querySelectorAll('.assistant-unverified:not([title="unverified"])') ?? []).map((m) => m.firstChild.textContent)`);
const findingRows = (s) => s.js(`Array.from(document.querySelectorAll(".assistant-findings li")).map((e) => e.innerText.replace(/\\s+/g, " ").trim()).filter(Boolean)`);
const lastCard = (s) => s.js(`Array.from(document.querySelectorAll(".assistant-card")).at(-1)?.innerText.replace(/\\n+/g, " / ") ?? ""`);
const checkScreen = (s) => s.js(`(() => { const d = document.querySelector(".shell-drawer"); const all = document.body.innerText; return d ? all.replace(d.innerText, "") : all; })()`);
const has = (text, ...words) => words.every((w) => text.includes(w));
// No engine id and no machine path on a card's face: the folded diff is not on its face.
const RAW = /\b(?:rail|w|dev|fam|mdl)\.[\w-]+|\b(?:feetech|dynamixel)\.[\w-]+|\/(?:private|Users|tmp|var)\/|free_air/;
// The engine's strings carry ids such as rail.5v; the scripted answers say them in plain words, as a good answer would.
const IDS = { "rail.5v": "the 5 V rail", "w.pack": "the pack wire", "w.servo-feed": "the servo feed wire", "dev.jetson": "the Jetson" };
const plainIds = (t) => t.replace(/\b(?:rail|w|dev)\.[\w-]+/g, (id) => IDS[id] ?? id);
/** Before a card's picture: its head in view (the panel scrolls to the end of the newest answer). */
const toCard = (s) => s.js(`Array.from(document.querySelectorAll(".assistant-card")).at(-1)?.scrollIntoView({ block: "start" })`);

/** Press the last card's Apply; the first time, give the saved name in the app's own field. Then wait for the page's own check. */
async function apply(s, name) {
  const before = await verdictOf(s);
  if (!(await s.click("Apply", `Array.from(document.querySelectorAll(".assistant-card")).at(-1)`))) throw new Error("no Apply button on the last card");
  await settle(500);
  if (await s.js(`!!document.querySelector(".assistant-name input")`)) {
    await s.page.fill(".assistant-name input", name);
    await s.click("Save");
  }
  await s.until("Applied", `Array.from(document.querySelectorAll(".assistant-card")).at(-1)?.textContent.includes("Applied")`, 90_000);
  for (const t0 = Date.now(); Date.now() - t0 < 30_000 && (await verdictOf(s)) === before; ) await settle(300); // the page re-checks
  await settle(1500);
  return verdictOf(s);
}

/** One step: records PASS or FAIL and never stops the walk. */
async function step(beat, name, fn) {
  try { await fn(); } catch (e) { record(beat, name, [], "", false, String(e.message ?? e).slice(0, 600)); }
}

// ---- the scripted model: every reply is written from the real tool results before it ----
const earlier = (body, name) => json(resultsOf({ messages: body.messages.slice(0, -2) }).find((x) => x.name === name) ?? { text: "" }) ?? {};
const findingsOf = (r) => (json(r.find((x) => x.name === "check") ?? { text: "" }) ?? {}).findings ?? [];
const fault = (fs, re) => fs.find((f) => re.test(f.quantity));
const firstFix = (r) => (json(r.find((x) => x.name === "suggest-fixes") ?? { text: "" }) ?? {}).fixes?.[0];

const SERVO_WHY = [
  () => use("check", {}),
  (r) => use("show-only", { finding: fault(findingsOf(r), /wrist\/limits\/effort/).quantity }),
  (_r, body) => {
    const fs = earlier(body, "check").findings ?? [];
    const p = fault(fs, /^budgets\/bud\.power/), w = fault(fs, /wrist\/limits\/effort/);
    return say(`Two faults stop this robot.\n\n- The wrist is allowed to push ${w.observed.split(" joint")[0]}, but its servo stalls at ${w.expected.replace("<= ", "").split(",")[0]}.\n- The 5 V servo rail can give ${p.expected.replace("<= ", "")}, and what is plugged into it wants ${p.observed}.\n\nThe robot now shows only the parts of the wrist fault.`);
  },
];
const SERVO_FIX = [
  () => use("suggest-fixes", { finding: "model/joints/wrist/limits/effort", limit: 2 }),
  (r) => use("swap", firstFix(r).steps[0].args),
  (_r, body) => {
    const f = earlier(body, "suggest-fixes").fixes[0];
    return say(`The engine searched the catalog and proved this change: swap the wrist servo for the ${f.part.name}, which stalls at ${f.part.numbers.stall_torque.value} ${f.part.numbers.stall_torque.unit}. With it the wrist fault is gone and no new fault appears.\n\nTwo things to know: it speaks a different bus, so it needs an adapter or a controller for that bus, and the robot stays NO-GO because the servo rail is still overloaded.\n\nThe card shows the check before and after. Nothing changes until you press Apply.`);
  },
];
const ARM_WHY = [
  () => use("check", {}),
  (r) => {
    const fs = findingsOf(r);
    const b = fault(fs, /brownout/), f = fault(fs, /fuse/), a = fault(fs, /ampacity/);
    return say(`Two faults stop this robot.\n\n- Brown-out: ${plainIds(b.observed.replace(/ \(.*\)/, ""))}, and the Jetson needs at least ${b.expected.replace(">= ", "").split(",")[0]}.\n- Fuse: a ${f.observed.split(" fuse")[0]} fuse sits on a wire that can carry only ${f.expected.replace("fuse <= ", "").replace(" wire ampacity", "")}.\n\nThere are also two warnings: the servo feed wire carries ${a.observed.split(" computed")[0]}, more than it is rated for, and the ESP32 uses a reserved pin.`);
  },
];
const fixFor = (finding, label) => [
  () => use("suggest-fixes", { finding, limit: 2 }),
  (r) => { const st = firstFix(r).steps[0]; return use(st.command, st.args); },
  (_r, body) => {
    const f = earlier(body, "suggest-fixes").fixes[0];
    return say(`The smallest proved fix: ${label(f)}. The engine checked the whole robot with it: ${plainIds(f.after.observed)}, and no new fault appears.\n\nThe card shows the check before and after. Nothing changes until you press Apply.`);
  },
];
const ARM_FIX = fixFor("topology/rails/rail.5v/brownout", (f) => `make the servo feed wire thicker, ${f.label.replace(/^wire [^:]+: /, "")}`);
const ARM_FUSE = fixFor("topology/wires/w.pack/fuse", (f) => f.label.replace(/^fuse on [^:]+: /, "change the fuse from ").replace(/, what.*/, ""));

// Beat 5's datasheet: the exam's generated one-page PDF for a made-up servo. A copy of datasheetPdf() in
// app/src/main/assistant-eval/datasheet.ts, kept here as a copy: no real maker's document.
const DATASHEET = "lw-t1-datasheet.pdf";
const LINES = ["Lockwire test servo LW-T1", "A made-up part for the Lockwire assistant exam. Not a real product.", "Weight 57 g",
  "Stall torque 0.52 N*m at 6 V", "Stall current 1.2 A at 6 V", "Operating voltage 4.8 V to 6 V", "No-load current 150 mA at 6 V"];
function datasheetPdf() {
  const esc = (s) => s.replace(/[\\()]/g, "\\$&");
  const stream = `BT /F1 12 Tf 16 TL 72 720 Td ${LINES.map((l) => `(${esc(l)}) Tj T*`).join(" ")} ET`;
  const objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>", `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`];
  let out = "%PDF-1.4\n";
  const at = [];
  objects.forEach((o, i) => { at.push(out.length); out += `${i + 1} 0 obj\n${o}\nendobj\n`; });
  const xref = out.length;
  out += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${at.map((n) => `${String(n).padStart(10, "0")} 00000 n \n`).join("")}`;
  out += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(out, "latin1");
}
const num = (name, value, unit, quote) => ({ name, value, unit, page: 1, quote });
const PART = [
  () => use("list-files", { glob: "*.pdf" }),
  () => use("read-file", { path: DATASHEET }),
  () => use("propose-part", { datasheet: DATASHEET, id: "dev.lw_t1", family: "servo", model: "LW-T1", owner: "you",
    numbers: [num("mass", 57, "g", "Weight 57 g"), num("stall_torque", 0.52, "N*m", "Stall torque 0.52 N*m at 6 V"), num("stall_current", 1.2, "A", "Stall current 1.2 A at 6 V"), num("voltage_max", 6, "V", "Operating voltage 4.8 V to 6 V")] }),
  (r) => say(r[0]?.error
    ? `I could not propose it: ${r[0].text.slice(0, 200)}`
    : "I read the datasheet and proposed the LW-T1 as a new device. Each number on the card carries its page and the line it came from: 57 g, 0.52 N*m at 6 V, 1.2 A at 6 V and 6 V.\n\nThe quotes are not checked against the file, so look at them before you press Apply."),
];

const NAME = "Sam Carter"; // a made-up test name, saved once per launch through the app's own field

async function servoBench(theme, pics = true) {
  const tag = pics ? theme : `${theme}, timing run`;
  const s = await launch(copyRobot("servo-bench.yaml"), theme);
  const pic = (n) => (pics ? s.shot(n) : Promise.resolve("no picture"));
  try {
    if (pics) await step("1", `servo bench, ${tag}`, async () => {
      const v = await verdictOf(s); const file = await pic("b1-servo-bench-check");
      record("1", `servo bench Check screen, ${tag}`, [], `${v}; ${file}`, has(v, "NO-GO"));
    });
    await s.allow();
    if (pics) await step("2", `why, ${tag}`, async () => {
      const tools = await s.ask("2", "Why is this NO-GO?", SERVO_WHY);
      const rows = await findingRows(s), a = await lastAnswer(s), u = await unverified(s), file = await pic("b2-why");
      record("2", `"Why is this NO-GO?", ${tag}`, tools, `rows: ${JSON.stringify(rows)}; answer: ${JSON.stringify(a)}; unverified marks: ${u}; marked as after the change: ${JSON.stringify(await proposedMarks(s))}; ${file}`, rows.length === 2 && u === 0);
    });
    if (pics) await step("2", `composer, ${tag}`, async () => {
      const set = (v) => s.js(`(() => { const t = document.querySelector('[aria-label="Ask about this robot"]'); Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(t, ${JSON.stringify(v)}); t.dispatchEvent(new Event("input", { bubbles: true })); })()`);
      // where the box, the round button and the arrow really are (hidden app, device-independent px); every gap must match within 0.5 px
      const geo = () => s.js(`(() => { const r = (e) => e.getBoundingClientRect(), t = r(document.querySelector('[aria-label="Ask about this robot"]')), b = document.querySelector('.assistant-send'), d = r(b), g = r(b.querySelector("path")), n = (x) => Math.round(x * 100) / 100;
        return { boxH: n(t.height), right: n(t.right - d.right), top: n(d.top - t.top), bottom: n(t.bottom - d.bottom), dy: n((t.top + t.bottom) / 2 - (d.top + d.bottom) / 2), gx: n((g.left + g.right) / 2 - (d.left + d.right) / 2), gy: n((g.top + g.bottom) / 2 - (d.top + d.bottom) / 2) }; })()`);
      const ok = (m) => Math.abs(m.right - m.top) <= 0.5 && Math.abs(m.top - m.bottom) <= 0.5 && Math.abs(m.dy) <= 0.5 && Math.abs(m.gx) <= 0.5 && Math.abs(m.gy) <= 0.5;
      let empty, typed, grown, m1, m2, m3;
      try {
        empty = await s.shot("v2-empty"); await settle(300); m1 = await geo();
        await set("Which servo should I swap?");
        typed = await s.shot("v2-typed"); await settle(300); m2 = await geo();
        await set("Which servo should I swap?\nAnd is the 5 V rail the cause?\nOr is it the wrist?");
        grown = await s.shot("v2-grown"); await settle(300); m3 = await geo();
      } finally { await set(""); }
      const grownOk = Math.abs(m3.right - m3.bottom) <= 0.5 && Math.abs(m3.gx) <= 0.5 && Math.abs(m3.gy) <= 0.5 && m3.boxH > m1.boxH;
      record("2", `the question box: the round button centred, empty / typed / three lines, ${tag}`, [], `empty ${JSON.stringify(m1)}; typed ${JSON.stringify(m2)}; grown ${JSON.stringify(m3)}; ${empty}; ${typed}; ${grown}`, ok(m1) && ok(m2) && grownOk);
    });
    if (pics) await step("2", `eye on the wrist, ${tag}`, async () => {
      const pressed = await s.js(`(() => { const bs = Array.from(document.querySelectorAll(".assistant-findings button")); const b = bs.find((b) => /wrist/i.test(b.title ?? "")) ?? bs.at(-1); b?.click(); return b?.title ?? ("no eye: " + document.querySelectorAll(".assistant-findings").length + " lists, " + document.querySelectorAll("button.kit-iconbtn").length + " icon buttons"); })()`);
      await settle(2000);
      const file = await pic("b2-eye-wrist");
      record("2", `eye on the wrist row, ${tag}`, [], `pressed "${pressed}"; ${file}`, !pressed.startsWith("no eye"));
    });
    await step("3", `smallest fix, ${tag}`, async () => {
      const tools = await s.ask("3", "What is the smallest fix?", SERVO_FIX);
      const card = await lastCard(s), a = await lastAnswer(s), u = await unverified(s), file = await toCard(s).then(() => pic("b3-fix-card"));
      record("3", `"What is the smallest fix?", ${tag}`, tools, `card: ${JSON.stringify(card)}; answer: ${JSON.stringify(a)}; unverified marks: ${u}; marked as after the change: ${JSON.stringify(await proposedMarks(s))}; ${file}`, has(card, "Proved by the engine", "Before", "After", "Swap the wrist servo", "Feetech SC-0090-C001 smart servo (SCS0009) → ROBOTIS Dynamixel XL330-M288-T smart servo") && !RAW.test(card) && u === 0);
    });
    if (pics) await step("3", `Apply, ${tag}`, async () => {
      const v = await apply(s, NAME), screen = await checkScreen(s), file = await pic("b3-applied");
      const wristLeft = /wrist/i.test(screen.split("Findings")[1] ?? "");
      record("3", `Apply (saved name "${NAME}"), ${tag}`, [], `verdict after: ${v}; "wrist" still in the Check screen's findings: ${wristLeft}; ${file}`, !wristLeft && /\b1 violation/i.test(v));
    });
  } finally { await s.close(); }
}

async function deskArm(theme, pics = true) {
  const tag = pics ? theme : `${theme}, timing run`;
  const s = await launch(copyRobot("arm-power"), theme);
  const pic = (n) => (pics ? s.shot(n) : Promise.resolve("no picture"));
  try {
    if (pics) await step("1", `desk arm, ${tag}`, async () => {
      const v = await verdictOf(s); const file = await pic("b1-desk-arm-check");
      record("1", `desk arm Check screen, ${tag}`, [], `${v}; ${file}`, has(v, "NO-GO"));
    });
    await step("4", `graph open, ${tag}`, async () => {
      const ok = await s.js(`(() => { const b = Array.from(document.querySelectorAll("button")).find((b) => b.textContent.trim() === "Graph" || b.getAttribute("aria-label") === "Graph"); b?.click(); return !!b; })()`);
      await settle(1500);
      if (pics) record("4", `Graph pressed, ${tag}`, [], `Graph button found: ${ok}; ${await pic("b4-graph")}`, ok);
    });
    await s.allow();
    if (pics) await step("4", `why, ${tag}`, async () => {
      const tools = await s.ask("4", "Why is this NO-GO?", ARM_WHY);
      const rows = await findingRows(s), a = await lastAnswer(s), u = await unverified(s), file = await pic("b4-why");
      record("4", `"Why is this NO-GO?", ${tag}`, tools, `rows: ${JSON.stringify(rows)}; answer: ${JSON.stringify(a)}; unverified marks: ${u}; marked as after the change: ${JSON.stringify(await proposedMarks(s))}; ${file}`, rows.length >= 2 && u === 0);
    });
    if (pics) await step("4", `eye on the brown-out, ${tag}`, async () => {
      const pressed = await s.js(`(() => { const bs = Array.from(document.querySelectorAll(".assistant-findings button")); const b = bs.find((b) => /brown|sag|rail/i.test(b.title ?? "")) ?? bs[0]; b?.click(); return b?.title ?? ("no eye: " + document.querySelectorAll(".assistant-findings").length + " lists, " + document.querySelectorAll("button.kit-iconbtn").length + " icon buttons"); })()`);
      await settle(2000);
      const file = await pic("b4-eye-brownout");
      record("4", `eye on the brown-out row, graph open, ${tag}`, [], `pressed "${pressed}"; ${file}`, !pressed.startsWith("no eye"));
    });
    await step("4", `smallest fix, ${tag}`, async () => {
      const tools = await s.ask("4", "What is the smallest fix?", ARM_FIX);
      const card = await lastCard(s), a = await lastAnswer(s), u = await unverified(s), file = await toCard(s).then(() => pic("b4-fix-card"));
      record("4", `"What is the smallest fix?", ${tag}`, tools, `card: ${JSON.stringify(card)}; answer: ${JSON.stringify(a)}; unverified marks: ${u}; marked as after the change: ${JSON.stringify(await proposedMarks(s))}; ${file}`, has(card, "Proved by the engine", "Change wire gauge on Servo feed", "28 AWG → 22 AWG") && !RAW.test(card) && u === 0);
    });
    await step("4", `Apply the wire, ${tag}`, async () => {
      const v = await apply(s, NAME), file = await pic("b4-wire-applied");
      record("4", `Apply the wire (saved name "${NAME}"), ${tag}`, [], `verdict after: ${v}; ${file}`, /\b1 violation/i.test(v));
    });
    await step("4", `and the fuse, ${tag}`, async () => {
      const tools = await s.ask("4", "And the fuse?", ARM_FUSE);
      const card = await lastCard(s), a = await lastAnswer(s), u = await unverified(s), file = await toCard(s).then(() => pic("b4-fuse-card"));
      record("4", `"And the fuse?", ${tag}`, tools, `card: ${JSON.stringify(card)}; answer: ${JSON.stringify(a)}; unverified marks: ${u}; marked as after the change: ${JSON.stringify(await proposedMarks(s))}; ${file}`, has(card, "Proved by the engine", "9 A → 7 A") && !RAW.test(card) && u === 0);
    });
    await step("4", `Apply the fuse, ${tag}`, async () => {
      const v = await apply(s, NAME), file = await pic("b4-fuse-applied");
      const card = await lastCard(s); // the applied card keeps its change line as it came
      record("4", `Apply the fuse, ${tag}`, [], `verdict after: ${v}; card keeps "9 A → 7 A": ${card.includes("9 A → 7 A")}; ${file}`, /^GO\b/.test(v) && card.includes("9 A → 7 A"));
    });
  } finally { await s.close(); }
}

/** Beat 5 and the new pieces (what can you do, describe, report, a made-up number, Change), servo bench, light. */
async function datasheetAndExtras() {
  const robot = copyRobot("servo-bench.yaml");
  writeFileSync(join(dirname(robot), DATASHEET), datasheetPdf());
  const s = await launch(robot, "light");
  try {
    await s.allow();
    await step("5", "datasheet", async () => {
      const tools = await s.ask("5", "Add this servo from its datasheet.", PART);
      const card = await lastCard(s), a = await lastAnswer(s), u = await unverified(s), file = await toCard(s).then(() => s.shot("b5-part-card"));
      record("5", `"Add this servo from its datasheet."`, tools, `card face: ${JSON.stringify(card)}; answer: ${JSON.stringify(a)}; unverified marks: ${u}; marked as after the change: ${JSON.stringify(await proposedMarks(s))}; ${file}`, has(card, "LW-T1", "mass: 57 g, p.1, “Weight 57 g”", "Apply") && !RAW.test(card) && u === 0);
    });
    await step("5", "the exact change, opened", async () => {
      await s.js(`Array.from(document.querySelectorAll(".assistant-card")).at(-1).querySelector("summary").click()`);
      await settle(300);
      await toCard(s);
      const card = await lastCard(s), file = await s.shot("b5-part-card-opened");
      record("5", `the card's "The exact change" opened`, [], `card: ${JSON.stringify(card)}; ${file}`, has(card, "57", "p.1", "--- servo-bench.yaml") && !/\/(?:private|Users|tmp|var)\//.test(card));
      await s.js(`Array.from(document.querySelectorAll(".assistant-card")).at(-1).querySelector("summary").click()`); // closed again
    });
    await step("5", "Apply the part", async () => {
      const v = await apply(s, NAME), file = await s.shot("b5-part-applied");
      record("5", `Apply the part (saved name "${NAME}")`, [], `verdict after: ${v}; card: ${JSON.stringify(await lastCard(s))}; ${file}`, true);
    });
    await step("6", "what can you do", async () => {
      const tools = await s.ask("6", "What can you do?", [() => use("what-can-you-do", {}), (r) => say(r[0].text.replace(/^<<<[^\n]*\n|\n?UNTRUSTED\S*>>>\s*$/g, "").split("\n").filter((l) => l.trim()).slice(0, 14).join("\n"))]);
      record("6", `"What can you do?"`, tools, `answer: ${JSON.stringify(await lastAnswer(s))}; ${await s.shot("b6-what-can-you-do")}`, true);
    });
    for (const [q, tool, pic] of [["Describe this robot.", "describe", "b6-describe"], ["Make a report of this robot.", "report", "b6-report"]]) {
      await step("6", tool, async () => {
        let got = null;
        const tools = await s.ask("6", q, [() => use(tool, {}), (r) => { got = r[0]; return say(r[0].error ? `That did not work: ${r[0].text.slice(0, 160)}` : tool === "report" ? "The engine built the robot's full report as a web page. It lists the verdict, every finding and the parts." : "The engine's description has one section per part: what it is, its model and family, and who owns it."); }]);
        record("6", `"${q}" (${tool} through the assistant)`, tools, `tool error: ${got?.error}; reply starts: ${JSON.stringify(got?.text.slice(0, 160))}; answer: ${JSON.stringify(await lastAnswer(s))}; ${await s.shot(pic)}`, got && !got.error && got.text.length > 500);
      });
    }
    await step("6", "made-up number", async () => {
      const tools = await s.ask("6", "How heavy is this robot?", [() => say("It weighs about 1.37 kg.")]);
      const u = await unverified(s);
      record("6", `a number the model made up ("about 1.37 kg", no tool)`, tools, `unverified marks: ${u}; ${await s.shot("b6-unverified")}`, u >= 1);
    });
    await step("6", "Change under Advanced", async () => {
      await s.js(`Array.from(document.querySelectorAll(".shell-drawer summary")).find((x) => x.textContent === "Advanced").click()`);
      await s.until("the Applying as line", `document.querySelector(".shell-drawer details[open]")?.textContent.includes("Applying as ${NAME}")`);
      await s.click("Change");
      await s.until("the name field under Advanced", `!!document.querySelector(".shell-drawer details[open] .assistant-name input")`);
      await s.page.fill(".shell-drawer details[open] .assistant-name input", "Sam Rivera");
      await s.click("Save");
      await s.until("the changed name", `document.querySelector(".shell-drawer details[open]")?.textContent.includes("Applying as Sam Rivera")`);
      await s.js(`document.querySelector(".shell-drawer details[open]").scrollIntoView({ block: "end" })`);
      record("6", `"Change" under Advanced`, [], `"Applying as ${NAME}" became "Applying as Sam Rivera"; ${await s.shot("b6-change-name")}`, true);
    });
  } finally { await s.close(); }
}

/** Beat 6 of the demo: the gate on a SIMULATED recording of our own arm with a planted fault (never a live robot). */
async function gate() {
  const robot = copyRobot("arm");
  for (const [name, fault] of [["sim-arm-fault", "True"], ["sim-arm-clean", "False"]]) {
    const made = spawnSync("uv", ["run", "--project", REPO, "python", "-c", `import sys; sys.path.insert(0, "tests/private"); from pathlib import Path; from sim_recordings import build; build("arm", ${fault}, Path(sys.argv[1]))`, join(dirname(robot), "recordings", name)], { cwd: REPO, encoding: "utf8" });
    if (made.status !== 0) { record("6g", `make the simulated recording ${name}`, [], "", false, made.stderr.slice(-400)); return; }
  }
  const s = await launch(robot, "light");
  try {
    await s.allow();
    for (const [rec, q, pic] of [["sim-arm-fault", "Run the go/no-go gate on this recording.", "b6-gate"], ["sim-arm-clean", "And on the clean recording?", "b6-gate-clean"]]) await step("6g", `gate ${rec}`, async () => {
      let got = null;
      const tools = await s.ask("6g", q, [
        () => use("list-files", { folder: "recordings" }),
        () => use("gate-recording", { recording: `recordings/${rec}`, context: "nominal-op" }),
        (r) => {
          got = r[0];
          // The reply is cut at 20,000 characters (it is about 20,600), so it is not whole JSON: read it the way a model reads text.
          const verdict = r[0].text.match(/"verdict":"([A-Z_-]+)"/)?.[1];
          const bad = [...r[0].text.matchAll(/\{"severity":"violation","quantity":"[^"]*","observed":"([^"]*)","expected":"([^"(]*)/g)].slice(0, 2);
          const sim = /simulated/i.test(r[0].text) ? "\n\nThis recording says it is simulated, not from a real robot." : "";
          return say(r[0].error ? `The gate did not run: ${r[0].text.slice(0, 200)}` : `The gate says ${verdict ?? "(no verdict in the reply)"}.${bad.map((m) => `\n- ${m[1]}, against ${m[2].trim()}.`).join("")}${sim}`);
        },
      ]);
      const a = await lastAnswer(s);
      const u = await unverified(s);
      record("6g", `"${q}" (simulated arm recording, ${rec.endsWith("fault") ? "planted fault" : "no planted fault"})`, tools, `tool error: ${got?.error}; the arm's own check: ${await verdictOf(s)}; reply has "simulated": ${/simulated/i.test(got?.text ?? "")}; reply length ${got?.text.length}, cut: ${/\[Cut:/.test(got?.text ?? "")}; answer: ${JSON.stringify(a)}; unverified marks: ${u}; ${await s.shot(pic)}`, !!got && !got.error && /^This recording is simulated/.test(got.text) && !got.text.includes(dirname(robot)) && (rec.endsWith("fault") ? /NO-GO/.test(a) && u === 0 : true));
    });
  } finally { await s.close(); }
}

/** Re-check at Apply: a lock beside its own model file. No shipped example has a URDF on disk, so the shipped arm's model is exported
 * as one (`lockwire export --format urdf`) and locked from it (`lockwire start`). A card is made, the URDF is then edited the way another tool
 * would, and Apply writes nothing the first time. */
async function recheck() {
  const shipped = copyRobot("arm"), folder = join(dirname(dirname(shipped)), "so-arm"), urdf = join(folder, "arm.urdf");
  mkdirSync(folder);
  const uv = (args) => spawnSync("uv", ["run", "--project", REPO, "lockwire", ...args], { cwd: folder, encoding: "utf8" });
  for (const args of [["export", shipped, "--format", "urdf", "--out", urdf], ["start", "arm.urdf"]]) {
    const made = uv(args);
    if (made.status !== 0) { record("7", `lockwire ${args[0]} the shipped arm`, [], "", false, made.stderr.slice(-400)); return; }
  }
  const robot = join(folder, "lockwire.yaml");
  const mass = () => readFileSync(robot, "utf8").match(/- name: base_link\n(?:\s*#.*\n)?\s+mass:\s*\{?\s*value: ([\d.]+)/)?.[1]; // block form as locked, flow form as declared
  const s = await launch(robot, "light");
  try {
    await s.allow();
    let before = "";
    await step("7", "card", async () => {
      const tools = await s.ask("7", "Set the base's mass to 0.16 kg: I weighed it.", [
        () => use("declare", { path: "model/links/base_link/mass", value: "0.16 kg", source: "kitchen scale" }),
        () => say("I proposed it. The card shows the check before and after. Nothing changes until you press Apply."),
      ]);
      before = await lastCard(s);
      record("7", "the card as made", tools, `card: ${JSON.stringify(before)}`, has(before, "Before", "After", "Apply"));
    });
    await step("7", "the model file changes, then Apply", async () => {
      writeFileSync(urdf, readFileSync(urdf, "utf8").replace('<mass value="0.147" />', '<mass value="0.16" />')); // another tool edits the URDF: the base link, the first mass in it
      if (!(await s.click("Apply", `Array.from(document.querySelectorAll(".assistant-card")).at(-1)`))) throw new Error("no Apply button on the last card");
      await settle(500);
      if (await s.js(`!!document.querySelector(".assistant-name input")`)) { await s.page.fill(".assistant-name input", NAME); await s.click("Save"); }
      await s.until("the re-check sentence", `!!Array.from(document.querySelectorAll(".assistant-card")).at(-1)?.querySelector("[role=status]")`, 60_000);
      await toCard(s);
      const card = await lastCard(s), file = await s.shot("b7-rechecked-card");
      record("7", "Apply after the model file changed: nothing written, the card re-checked", [], `card: ${JSON.stringify(card)}; base mass in the lock: ${mass()}; ${file}`, card !== before && has(card, "The robot changed since this card was made", "Apply") && !card.includes("Applied") && mass() === "0.147");
      before = card;
    });
    await step("7", "the model file changes AGAIN, then Apply (a stale card is refused)", async () => {
      writeFileSync(urdf, readFileSync(urdf, "utf8").replace('<mass value="0.16" />', '<mass value="0.17" />'));
      await s.click("Apply", `Array.from(document.querySelectorAll(".assistant-card")).at(-1)`);
      for (const t0 = Date.now(); Date.now() - t0 < 60_000 && (await lastCard(s)) === before; ) await settle(300);
      const card = await lastCard(s);
      record("7", "a second model file change between presses: nothing written, re-shown again", [], `card: ${JSON.stringify(card)}; base mass in the lock: ${mass()}`, card !== before && has(card, "The robot changed since this card was made", "Apply") && !card.includes("Applied") && mass() === "0.147");
    });
    await step("7", "Apply again", async () => {
      const v = await apply(s, NAME), card = await lastCard(s);
      record("7", "the next Apply writes", [], `verdict after: ${v}; card: ${JSON.stringify(card)}; base mass in the lock: ${mass()}`, card.includes("Applied") && mass() === "0.16");
    });
    await step("7", "the model file is DELETED after a card is made, then Apply (a missing file is refused)", async () => {
      const lock = readFileSync(robot, "utf8");
      await s.ask("7", "Set the shoulder link's mass to 0.11 kg.", [
        () => use("declare", { path: "model/links/shoulder_link/mass", value: "0.11 kg", source: "kitchen scale" }),
        () => say("I proposed it. Nothing changes until you press Apply."),
      ]);
      const made = await lastCard(s);
      rmSync(urdf);
      await s.click("Apply", `Array.from(document.querySelectorAll(".assistant-card")).at(-1)`);
      for (const t0 = Date.now(); Date.now() - t0 < 60_000 && (await lastCard(s)) === made; ) await settle(300);
      const card = await lastCard(s), written = readFileSync(robot, "utf8") !== lock;
      record("7", "model file deleted: nothing written silently", [], `card as made: ${JSON.stringify(made)}; after Apply: ${JSON.stringify(card)}; lock written: ${written}`, !written && !card.includes("Applied") && /changed since this card|could not redo/.test(card));
    });
  } finally { await s.close(); }
}

/** Two cards of one turn on a shipped robot. After the first is applied, the second's re-shown Before and After against a
 * comparison made by hand with the engine's own command line on a copy of the robot as it now is. */
async function twoCards() {
  const robot = copyRobot("arm-power");
  const WIRE = { path: "topology/wires/w.servo-feed/awg", value: "22", source: "design choice: 22 AWG" };
  const FUSE = { path: "topology/rails/rail.battery/current_limit", value: "7 A", source: "design choice: a fuse at the wire's ampacity" };
  const s = await launch(robot, "light");
  const card = (i) => s.js(`document.querySelectorAll(".assistant-card")[${i}]?.innerText.replace(/\\n+/g, " / ") ?? ""`);
  try {
    await s.allow();
    await step("8", "two cards, one turn", async () => {
      await s.ask("8", "Fix the wire and the fuse.", [() => use("declare", WIRE), () => use("declare", FUSE), () => say("Two cards. Nothing changes until you press Apply.")]);
      const first = await card(0), second = await card(1);
      if (!(await s.click("Apply", `document.querySelectorAll(".assistant-card")[0]`))) throw new Error("no Apply on the first card");
      await settle(500);
      if (await s.js(`!!document.querySelector(".assistant-name input")`)) { await s.page.fill(".assistant-name input", NAME); await s.click("Save"); }
      await s.until("the first card applied", `document.querySelectorAll(".assistant-card")[0]?.textContent.includes("Applied")`, 90_000);
      await settle(1500);
      const waiting = await card(1); // the second card once the first is applied (saving the name adds "as <name>" to it)
      await s.click("Apply", `document.querySelectorAll(".assistant-card")[1]`);
      for (const t0 = Date.now(); Date.now() - t0 < 60_000 && (await card(1)) === waiting; ) await settle(300);
      const reshown = await card(1);
      // By hand: the robot as it now is, and a copy with the second change, each checked by the engine's command line.
      const hand = join(mkdtempSync(join(tmpdir(), "lw-demo-hand-")), "arm-power");
      cpSync(dirname(robot), hand, { recursive: true });
      const cli = (args) => spawnSync("uv", ["run", "--project", REPO, "lockwire", ...args], { cwd: hand, encoding: "utf8" });
      cli(["check", "contract.yaml", "--json", "before.json"]);
      cli(["declare", "contract.yaml", FUSE.path, FUSE.value, "--source", FUSE.source, "--witness", NAME]);
      cli(["check", "contract.yaml", "--json", "after.json"]);
      const [b, a] = ["before.json", "after.json"].map((f) => JSON.parse(readFileSync(join(hand, f), "utf8")));
      const key = (f) => [f.quantity, f.severity, f.observed, f.expected].join("\u0000");
      const bs = new Set(b.findings.map(key)), as = new Set(a.findings.map(key));
      const added = a.findings.filter((f) => !bs.has(key(f))), gone = b.findings.filter((f) => !as.has(key(f)));
      const changed = added.filter((f) => gone.some((g) => g.quantity === f.quantity)).length;
      const byHand = { before: b.verdict, after: a.verdict, new: added.length - changed, cleared: gone.length - changed, changed };
      const words = { GO_WITH_NOTES: "GO with notes" };
      const [, cb, ca] = reshown.match(/Before \/ ([^/]+?) \/.*After \/ ([^/]+?) \//) ?? [];
      const lines = reshown.split(" / After / ")[1]?.split(" / The ")[0].split(" / ").slice(1) ?? [];
      const onCard = { before: cb, after: ca, new: lines.filter((l) => l.startsWith("New: ")).length, cleared: lines.filter((l) => l.endsWith(": cleared")).length };
      const same = onCard.before === (words[byHand.before] ?? byHand.before) && onCard.after === (words[byHand.after] ?? byHand.after) && onCard.new === byHand.new && onCard.cleared === byHand.cleared;
      record("8", "the second card's re-shown Before and After against a hand-made engine comparison", [], `first card as made: ${JSON.stringify(first)}; second card as made: ${JSON.stringify(second)}; second card re-shown: ${JSON.stringify(reshown)}; by hand: ${JSON.stringify(byHand)}; on the card: ${JSON.stringify(onCard)}; equal: ${same}`, reshown !== waiting && has(reshown, "The robot changed since this card was made") && same);
    });
  } finally { await s.close(); }
}

const RUNS = [["b4", () => deskArm("light")], ["b4", () => deskArm("dark")], ["b4t", () => deskArm("light", false)],
  ["b2", () => servoBench("light")], ["b2", () => servoBench("dark")], ["b2t", () => servoBench("light", false)],
  ["b5", datasheetAndExtras], ["b6", gate], ["b7", recheck], ["b8", twoCards]];
for (const [key, run] of RUNS) {
  if (ONLY && !ONLY.includes(key)) continue;
  try { await run(); } catch (e) { record(key, "launch", [], "", false, String(e.message ?? e).slice(0, 600)); }
}
record("all", "no request to the model carries an absolute path", [], leaks.length ? JSON.stringify(leaks.slice(0, 3)) : "none in any beat", leaks.length === 0);
console.log("\nTimings as the app sees them (the model's tool call reaching the app, to the app's next request with the result; a card's wait includes its before-and-after check):");
for (const t of timings.filter((t) => /^(suggest-fixes|swap|declare|propose-part|gate-recording)/.test(t.tool))) console.log(`  beat ${t.beat}: ${t.tool}: ${(t.ms / 1000).toFixed(2)} s`);
if (process.env.DEMO_WALK_DUMP) writeFileSync(process.env.DEMO_WALK_DUMP, JSON.stringify({ results, timings, dumps }, null, 1));
const failed = results.filter((r) => !r.pass);
console.log(`\n${results.length - failed.length} passed, ${failed.length} failed`);
process.exitCode = failed.length ? 1 : 0;

