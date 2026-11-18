// The Assistant panel's pictures: node scripts/assistant-shots.mjs <out-dir>     (run `npm run build` first)
// Drives the built app, hidden, once per theme, 1280x800, on a temp copy of the servo bench, with a fresh user-data
// folder and a fresh saved-name file. The start screen is never captured. The engine is the real one.
//
// The model's replies are SCRIPTED, not live. The app has no switch for that and gains none: this script replaces `fetch`
// in the app's main process from outside (the way the unit tests stub it), answers only the Anthropic address from the
// queue below and passes every other request (the engine's) through. It also stands in for the keychain, so a made-up
// key is "saved" without touching the real one. Every picture is checked against the number of model calls it needed.
// The fix card is the real pipeline: the engine's suggest-fixes reply reaches the app as it is, and the card shows "Proved by the engine".
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { launchApp, stubDialogs, waitForSidecar } from "./e2e-drive.mjs";

const APP = join(dirname(fileURLToPath(import.meta.url)), "..");
const outDir = process.argv[2];
if (!outDir) throw new Error("usage: node scripts/assistant-shots.mjs <out-dir>");
mkdirSync(outDir, { recursive: true });
const settle = (ms) => new Promise((r) => setTimeout(r, ms));

const WRIST = "model/joints/wrist/limits/effort";
const use = (name, input) => ({ content: [{ type: "tool_use", id: `t-${name}`, name, input }], stop_reason: "tool_use" });
const say = (text) => ({ content: [{ type: "text", text }], stop_reason: "end_turn" });
// One list per question: the tool calls the model would make, then its answer. "about 6.4 A" is arithmetic of the model's own, so main marks it unverified.
const WHY = [use("show-only", { finding: WRIST }), use("select-part", { id: "joint:wrist" }),
  say("Two things stop this robot.\n\nThe wrist is allowed to push 0.40 N*m, but its servo stalls at 0.226 N*m. The servo rail can give 15 W, and what is plugged into it wants 32.1 W, which is about 6.4 A.\n\nI have selected the wrist on the robot so you can see where it is.")];
const SWAP = use("swap", { joint: "wrist", part_id: "dynamixel.xl330-m288" });
const PROVED = [use("suggest-fixes", { finding: WRIST, limit: 2 }), SWAP,
  say("The engine searched the catalog for a servo that clears the wrist fault and adds no new one. The smallest change is the ROBOTIS Dynamixel XL330-M288-T, which stalls at 0.52 N*m.\n\nThe card shows the check before and after.")];
const RAIL = [use("declare", { path: "devices/dev.servo1/power/draw", value: "2.5 W", source: "MG996R datasheet, running current" }),
  say("If the datasheet gives a lower running draw for servo 1, you can record it. This card sets it to 2.5 W.")];

async function run(theme) {
  const tmp = mkdtempSync(join(tmpdir(), "lw-assistant-shots-"));
  const robotDir = join(tmp, "servo-bench");
  mkdirSync(robotDir);
  const robot = join(robotDir, "servo-bench.yaml"); // a copy: Apply writes it, and a card's comparison works beside it
  copyFileSync(join(APP, "../src/lockwire/examples/servo-bench.yaml"), robot);
  writeFileSync(join(tmp, "config.toml"), ""); // no saved name yet: the first Apply asks for it
  const { app, page } = await launchApp({
    args: [".", "--user-data-dir=" + join(tmp, "ud")], cwd: APP,
    env: { LOCKWIRE_WITNESS_CONFIG: join(tmp, "config.toml"), LOCKWIRE_SIDECAR_COMMAND: "uv", LOCKWIRE_SIDECAR_ARGS: "run --project " + join(APP, "..") + " lockwire serve --exit-on-stdin-close" },
  });
  const js = (s) => page.evaluate(s);
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e).slice(0, 300)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text().slice(0, 600)); });
  const until = async (what, cond, ms = 60_000) => {
    for (const t0 = Date.now(); Date.now() - t0 < ms; await settle(150)) if (await js(cond)) return;
    throw new Error(`${theme}: timed out waiting for ${what}. The page reads: ${(await js(`document.body.textContent`)).slice(-600)}. Page errors: ${errors.join(" | ") || "none"}`);
  };
  const click = (name) => js(`(() => { const b = Array.from(document.querySelectorAll("button")).find((b) => b.textContent.trim() === ${JSON.stringify(name)}); b?.click(); return !!b; })()`);
  const shot = async (name) => {
    await settle(700);
    if (await js(`!!document.querySelector(".open-face")`)) throw new Error("the start screen is showing: not captured");
    const bytes = Buffer.from(await app.evaluate(async ({ BrowserWindow }) => Array.from((await BrowserWindow.getAllWindows()[0].webContents.capturePage()).toPNG())));
    writeFileSync(join(outDir, `${name}-${theme}.png`), bytes);
    console.log(`${name}-${theme}.png`);
  };
  const posts = () => app.evaluate(() => globalThis.__lwPosts);
  // One question: queue the scripted replies, ask, wait for the answer, and hold the model to exactly that many calls.
  const ask = async (text, replies) => {
    const before = await posts();
    const answers = await js(`document.querySelectorAll('.assistant-turn[data-who="assistant"]').length`);
    await app.evaluate((_, r) => { globalThis.__lwReplies.push(...r); }, replies);
    await page.fill('[aria-label="Ask about this robot"]', text);
    await page.press('[aria-label="Ask about this robot"]', "Enter");
    await until(`the answer to "${text}"`, `document.querySelectorAll('.assistant-turn[data-who="assistant"]').length === ${answers + 1}`);
    if ((await posts()) - before !== replies.length) throw new Error(`"${text}": the model stand-in got ${(await posts()) - before} calls, expected ${replies.length}`);
  };
  try {
    await app.evaluate(({ BrowserWindow, safeStorage }) => {
      BrowserWindow.getAllWindows()[0].setContentSize(1280, 800);
      safeStorage.isEncryptionAvailable = () => true;
      safeStorage.encryptString = (s) => Buffer.from(`shots:${s}`);
      safeStorage.decryptString = (b) => b.toString().slice(6);
      const real = globalThis.fetch;
      globalThis.__lwReplies = [];
      globalThis.__lwPosts = 0;
      globalThis.fetch = async (url, init) => {
        if (String(url) !== "https://api.anthropic.com/v1/messages") return real(url, init);
        globalThis.__lwPosts += 1;
        const reply = globalThis.__lwReplies.shift();
        return new Response(JSON.stringify(reply ?? { type: "error", error: { message: "no scripted reply left" } }), { status: reply ? 200 : 500, headers: { "content-type": "application/json" } });
      };
    });
    await stubDialogs(app, { robot });
    await js(`localStorage.setItem("lockwire-theme", "${theme}")`);
    await page.reload(); await page.waitForLoadState("load"); await waitForSidecar(page);
    await until("Open robot", `!!Array.from(document.querySelectorAll("button")).find((b) => b.textContent === "Open robot")`);
    await click("Open robot");
    await until("the check", `!!document.querySelector(".check-headline:not(.verdict-pending)")`);
    await settle(2500); // the 3D view settles
    await click("Assistant");
    await until("the allow screen", `!!document.querySelector('.shell-drawer input[type="password"]')`);
    await shot("e-allow");
    await page.fill('.shell-drawer input[type="password"]', "sk-ant-made-up-for-pictures");
    await click("Save key and allow for this robot");
    await until("the question box", `!!document.querySelector('[aria-label="Ask about this robot"]')`);

    await ask("Why is this NO-GO?", WHY);
    await shot("a-why");
    await ask("What is the smallest fix?", PROVED);
    if (!(await js(`document.querySelector(".assistant-card")?.textContent.includes("Proved by the engine")`))) throw new Error(`${theme}: the real fix card has no "Proved by the engine"`);
    await shot("b-fix-card");
    await click("Apply");
    await until("the name field", `!!document.querySelector(".assistant-name input")`);
    await shot("c-first-apply-name");
    await page.fill(".assistant-name input", "Sam Carter");
    await click("Save");
    await until("Applied", `document.querySelector(".assistant-card")?.textContent.includes("Applied")`);
    await settle(2500); // the page re-checks
    await shot("f-applied");
    chmodSync(robotDir, 0o555); // the comparison's copy cannot be made beside the file: a real "could not compare"
    await ask("And the servo rail?", RAIL);
    chmodSync(robotDir, 0o755);
    await shot("d-not-compared");

    // One assistant: the drawer is Assistant | Terminal, and a Check, More button that once started the old Agent tab
    // sends main's question in this panel, the way a typed question goes.
    const answered = await js(`document.querySelectorAll('.assistant-turn[data-who="assistant"]').length`);
    const sentBefore = await posts();
    await app.evaluate((_, r) => { globalThis.__lwReplies.push(...r); }, [say("Two real problems, one cause each.\n\nThe wrist servo is too weak for the effort the wrist is allowed. The servo rail cannot feed what is plugged into it.\n\nAsk for the smallest fix and I will propose it as a card.")]);
    if (!(await click("Review my robot"))) throw new Error(`${theme}: no Review my robot button`);
    await until("the review answer", `document.querySelectorAll('.assistant-turn[data-who="assistant"]').length === ${answered + 1}`);
    if ((await posts()) - sentBefore !== 1) throw new Error(`${theme}: the review question took ${(await posts()) - sentBefore} model calls, expected 1`);
    const tabs = await js(`Array.from(document.querySelectorAll('.shell-drawer [role="tab"]')).map((t) => t.textContent).join(" | ")`);
    if (tabs !== "Assistant | Terminal" || /\bAgent\b/.test(await js(`document.querySelector(".shell-drawer").innerText`))) throw new Error(`${theme}: the drawer reads ${tabs}`);
    await shot("i-one-assistant");

    // The saved name, and the way to change it: under Advanced, one line.
    await js(`Array.from(document.querySelectorAll(".shell-drawer summary")).find((s) => s.textContent === "Advanced").click()`);
    await until("the Applying as line", `document.querySelector(".shell-drawer details[open]")?.textContent.includes("Applying as Sam Carter")`);
    await js(`document.querySelector(".shell-drawer .kit-panel-footer, .shell-drawer details[open]").scrollIntoView({ block: "end" })`);
    await shot("h-applying-as");
    await click("Change"); // the same field the first Apply uses; saving it changes the line and applies nothing
    await until("the name field under Advanced", `!!document.querySelector(".shell-drawer details[open] .assistant-name input")`);
    await page.fill(".shell-drawer details[open] .assistant-name input", "Sam Rivera");
    await click("Save");
    await until("the changed name", `document.querySelector(".shell-drawer details[open]")?.textContent.includes("Applying as Sam Rivera")`); // Advanced stays open for the checks below

    // The footer tag: in Design a selected part rides with the next question, and the cross takes it off.
    await js(`Array.from(document.querySelectorAll('.shell-tabs [role="tab"]')).find((t) => t.textContent === "Design")?.click()`);
    await until("the parts list", `!!Array.from(document.querySelectorAll(".kit-tree-row")).find((r) => /wrist/i.test(r.textContent))`);
    await js(`(() => { const rows = Array.from(document.querySelectorAll(".kit-tree-row")); (rows.find((r) => /^wrist$/i.test(r.textContent.trim())) ?? rows.find((r) => /wrist/i.test(r.textContent))).click(); })()`);
    await until("the selection tag", `!!document.querySelector(".assistant-tag")`);
    await shot("g-selection-tag");

    // Not pictures: nothing a person reads may be a path, a command name or a long dash; the panel must fit a narrow drawer; every control shows its focus.
    const words = await js(`document.querySelector(".shell-drawer").innerText`);
    for (const bad of [/[–—]/, /\bsign/i, /model\/joints|budgets\/|topology\//, /swap-apply|suggest-fixes|show-only|select-part/]) if (bad.test(words)) throw new Error(`${theme}: the panel shows ${bad}: ${words.match(bad)[0]}`);
    await page.keyboard.press("Shift");
    const ringless = await js(`Array.from(document.querySelectorAll(".shell-drawer .drawer-body:not([hidden]) :is(button, textarea, input, summary)")).filter((e) => !e.disabled && e.offsetWidth).filter((e) => { e.focus(); const c = getComputedStyle(e); return document.activeElement === e && (c.outlineStyle === "none" || parseFloat(c.outlineWidth) < 2); }).map((e) => e.textContent || e.getAttribute("aria-label"))`);
    if (ringless.length) throw new Error(`${theme}: no focus ring on ${ringless.join(", ")}`);
    for (const width of [800, 640]) {
      await app.evaluate(({ BrowserWindow }, w) => BrowserWindow.getAllWindows()[0].setContentSize(w, 800), width);
      await settle(600);
      const over = await js(`Array.from(document.querySelectorAll(".shell-drawer .drawer-body:not([hidden]), .shell-drawer .kit-panel-body, .shell-drawer .kit-panel-footer, .shell-drawer .assistant-card")).filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => e.className + " " + e.scrollWidth + ">" + e.clientWidth)`);
      console.log(`${theme}: window ${width} wide, drawer ${await js(`document.querySelector(".shell-drawer").offsetWidth`)} wide, sideways overflow: ${over.length ? over.join("; ") : "none"}`);
      if (over.length) throw new Error(`${theme}: the panel overflows at ${width}`);
    }
    console.log(`${theme}: model calls answered by the stand-in: ${await posts()}; focus rings and plain words checked`);
  } finally {
    try { chmodSync(robotDir, 0o755); } catch { /* already gone */ }
    await app.close().catch(() => {});
  }
}

for (const theme of ["light", "dark"]) await run(theme);
