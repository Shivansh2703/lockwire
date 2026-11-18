// 3D soak: cd app && npm run build && TMPDIR=<scratch> node scripts/soak-3d.mjs [rounds=30 | risky] [robot...]   (exit 1 on any event)
// Pre-flight for a recording: drives the built app HIDDEN (e2e-drive's launchApp: no window, no Dock icon) over copies of
// the shipped starters, real engine, scripted model replies (no key, no network), and counts what would lose a take:
// renderer/GPU process gone, WebGL context lost, uncaught errors and rejections (renderer and main), a blank 3D canvas
// (every uncovered canvas pixel one colour while a robot is open), and memory at the start and end of every cycle.
// A round visits each robot once; servo-bench and arm-power every round, the others in the first 5. Every visit is a
// switch (File > Open Robot, same app) and one full cycle: Design, Check, Plan, Test, Run; graph on/off; connections lens
// on/off; select every part; each finding's eye in the Assistant panel then show all; light/dark; four window sizes;
// one scripted declare card applied and re-checked. The start screen is never captured (nothing is captured to disk).
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { launchApp, stubDialogs, waitForSidecar } from "./e2e-drive.mjs";

const APP = join(dirname(fileURLToPath(import.meta.url)), "..");
const DEMO = ["servo-bench", "arm-power"];
const OTHERS = ["arm", "drone", "hexapod-param", "humanoid", "quadruped", "rover", "rover-param"];
const [roundsArg, ...only] = process.argv.slice(2);
const ROUNDS = roundsArg === "risky" ? 1 : Number(roundsArg ?? 30);
const OTHER_ROUNDS = Math.min(5, ROUNDS);
const settle = (ms) => new Promise((r) => setTimeout(r, ms));

/** Copy the starters into one temp folder; `extra` maps a name to a ready-made contract path (risky inputs). */
export function copyStarters(tmp, names) {
  // Written by the engine's own starter writer, as "New robot" does: <name>.yaml with its scene as <name>.glb beside it
  // (a contract.yaml next to scene.glb would not find its scene, and the 3D view would fall back to the skeleton).
  const robots = Object.fromEntries(names.map((name) => { mkdirSync(join(tmp, name)); return [name, join(tmp, name, `${name}.yaml`)]; }));
  const py = "import sys\nfrom pathlib import Path\nfrom lockwire.start_operation import write_example\nfor n, p in zip(sys.argv[1::2], sys.argv[2::2]): write_example(n, Path(p))";
  execFileSync("uv", ["run", "--project", join(APP, ".."), "python", "-c", py, ...Object.entries(robots).flat()]);
  return robots;
}

/** A declarable number of the robot (path, value, unit), from the engine's own declare form, for the scripted card. */
function declarable(contract) {
  const py = "import json,sys\nfrom lockwire.api import declare_form\nr=[x for x in declare_form(sys.argv[1])['rows'] if isinstance(x.get('value'),(int,float)) and not isinstance(x.get('value'),bool) and x.get('unit')]\nprint(json.dumps({k: r[0][k] for k in ('path','value','unit')} if r else None))";
  return JSON.parse(execFileSync("uv", ["run", "--project", join(APP, ".."), "python", "-c", py, contract], { encoding: "utf8" }).trim().split("\n").at(-1));
}

/** Launch hidden with the model stand-in, the crash hooks and the event counters. Returns the driver. */
export async function startSoakApp(tmp, firstRobot) {
  writeFileSync(join(tmp, "config.toml"), 'witness = "Soak"\n');
  const { app, page } = await launchApp({
    args: [".", "--user-data-dir=" + join(tmp, "ud"), ...(process.env.SOAK_GC ? ["--js-flags=--expose-gc"] : [])], cwd: APP,
    env: { LOCKWIRE_WITNESS_CONFIG: join(tmp, "config.toml"), LOCKWIRE_SIDECAR_COMMAND: "uv", LOCKWIRE_SIDECAR_ARGS: "run --project " + join(APP, "..") + " lockwire serve --exit-on-stdin-close" },
  });
  const events = { rendererGone: [], childGone: [], mainUncaught: [], pageErrors: [], consoleErrors: [], contextLost: 0, contextRestored: 0, rendererUncaught: [], rendererRejections: [], blank: [], samples: 0, driver: [], nativeDialogs: [], showAllMissed: [], sizes: [] };
  page.on("pageerror", (e) => events.pageErrors.push(String(e).slice(0, 300)));
  page.on("console", (m) => { if (m.type() === "error") events.consoleErrors.push(m.text().slice(0, 300)); });
  await app.evaluate(({ app: eapp, BrowserWindow, safeStorage, dialog }) => {
    const hidden = BrowserWindow.getAllWindows()[0];
    globalThis.__soak = { rendererGone: [], childGone: [], mainUncaught: [], dialogs: [], visible: [hidden.isVisible(), eapp.dock?.isVisible() ?? false] };
    // A native message box would show on the screen even with the window hidden: none may open, each is counted.
    dialog.showMessageBox = async (...a) => { globalThis.__soak.dialogs.push(String(a.at(-1)?.message).slice(0, 200)); return { response: a.at(-1)?.cancelId ?? 0, checkboxChecked: false }; };
    dialog.showMessageBoxSync = (...a) => { globalThis.__soak.dialogs.push(String(a.at(-1)?.message).slice(0, 200)); return a.at(-1)?.cancelId ?? 0; };
    dialog.showErrorBox = (title, content) => { globalThis.__soak.dialogs.push(`${title}: ${content}`.slice(0, 200)); };
    eapp.on("render-process-gone", (_e, _wc, d) => globalThis.__soak.rendererGone.push(JSON.stringify(d)));
    eapp.on("child-process-gone", (_e, d) => globalThis.__soak.childGone.push(JSON.stringify(d)));
    process.on("uncaughtException", (e) => globalThis.__soak.mainUncaught.push("uncaught: " + String(e?.stack ?? e).slice(0, 300)));
    process.on("unhandledRejection", (e) => globalThis.__soak.mainUncaught.push("rejection: " + String(e?.stack ?? e).slice(0, 300)));
    safeStorage.isEncryptionAvailable = () => true; // a made-up key, never the real keychain
    safeStorage.encryptString = (s) => Buffer.from(`soak:${s}`);
    safeStorage.decryptString = (b) => b.toString().slice(5);
    globalThis.__lwShows = 0; // every show message main sends the page, counted
    const wc = hidden.webContents, send = wc.send.bind(wc);
    wc.send = (channel, ...rest) => { if (channel === "lockwire:assistant-show") globalThis.__lwShows += 1; return send(channel, ...rest); };
    const real = globalThis.fetch;
    globalThis.__lwReplies = [];
    globalThis.fetch = async (url, init) => {
      if (String(url) !== "https://api.anthropic.com/v1/messages") return real(url, init); // the engine's calls pass through
      const reply = globalThis.__lwReplies.shift();
      return new Response(JSON.stringify(reply ?? { type: "error", error: { message: "no scripted reply left" } }), { status: reply ? 200 : 500, headers: { "content-type": "application/json" } });
    };
  });
  const js = (s) => page.evaluate(s);
  const hooks = `(() => { if (window.__soak) return; window.__soak = { lost: 0, restored: 0, errors: [], rejections: [] };
    addEventListener("webglcontextlost", () => window.__soak.lost++, true); addEventListener("webglcontextrestored", () => window.__soak.restored++, true);
    addEventListener("error", (e) => window.__soak.errors.push(String(e.message).slice(0, 300)));
    addEventListener("unhandledrejection", (e) => window.__soak.rejections.push(String(e.reason?.stack ?? e.reason).slice(0, 300))); })()`;
  const until = async (what, cond, ms = 90_000) => {
    for (const t0 = Date.now(); Date.now() - t0 < ms; await settle(150)) if (await js(cond)) return;
    throw new Error(`timed out waiting for ${what}`);
  };
  const click = (name) => js(`(() => { const b = Array.from(document.querySelectorAll("button")).find((b) => b.textContent.trim() === ${JSON.stringify(name)} && !b.disabled); b?.click(); return !!b; })()`);
  const tab = (name) => js(`Array.from(document.querySelectorAll('.shell-tabs [role="tab"]')).find((t) => t.textContent === ${JSON.stringify(name)})?.click()`);
  const size = async (w, h) => {
    const got = await app.evaluate(({ BrowserWindow }, [w, h]) => { const win = BrowserWindow.getAllWindows()[0]; win.setContentSize(w, h); return win.getContentSize(); }, [w, h]);
    if (got[0] !== w || got[1] !== h) events.sizes.push(`asked ${w}x${h}, got ${got[0]}x${got[1]}`);
  };
  // Collect the renderer-side counters into `events` (they live in the page and die with it).
  const harvest = async () => {
    const s = await js(`(() => { const s = window.__soak ?? { lost: 0, restored: 0, errors: [], rejections: [] }; window.__soak = { ...s, lost: 0, restored: 0, errors: [], rejections: [] }; return s; })()`).catch(() => null);
    if (s) { events.contextLost += s.lost; events.contextRestored += s.restored; events.rendererUncaught.push(...s.errors); events.rendererRejections.push(...s.rejections); }
    const m = await app.evaluate(({ app: eapp, BrowserWindow }) => { const s = globalThis.__soak; return { r: s.rendererGone.splice(0), c: s.childGone.splice(0), u: s.mainUncaught.splice(0), d: s.dialogs.splice(0), shown: BrowserWindow.getAllWindows().some((w) => w.isVisible()) || (eapp.dock?.isVisible() ?? false) }; });
    events.rendererGone.push(...m.r); events.childGone.push(...m.c); events.mainUncaught.push(...m.u); events.nativeDialogs.push(...m.d);
    if (m.shown) { await app.close().catch(() => {}); throw new Error("STOP: a window or the Dock icon became visible"); }
  };
  // Blank check: every canvas pixel no UI covers (8 px grid), read from the window's own capture; one colour = blank.
  const sample = async (where, retry = false) => {
    if (await js(`!!document.querySelector(".open-face")`)) throw new Error("the start screen is showing");
    // The 3D view's own drawing buffer (it keeps it: preserveDrawingBuffer), read back every 4th pixel: CSS layers over
    // the canvas (the vignette, labels) are not in it, so "every pixel one colour" means exactly that nothing is drawn.
    const s = await js(`(() => { const c = Array.from(document.querySelectorAll("canvas")).find((c) => c.offsetWidth > 200); if (!c) return null;
      const gl = c.getContext("webgl2") ?? c.getContext("webgl"); if (!gl) return { lost: true, share: 0, w: c.width, h: c.height }; if (gl.isContextLost()) return { lost: true, share: 0, w: c.width, h: c.height };
      const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight, px = new Uint8Array(w * h * 4); gl.readPixels(0, 0, w, h, gl.RGBA, gl.UNSIGNED_BYTE, px);
      const count = new Map(); let n = 0; for (let i = 0; i < px.length; i += 16) { const k = px[i] * 16777216 + px[i + 1] * 65536 + px[i + 2] * 256 + px[i + 3]; count.set(k, (count.get(k) ?? 0) + 1); n += 1; }
      return { lost: false, share: 1 - Math.max(...count.values()) / n, colours: count.size, w, h }; })()`);
    if (!s) { // no 3D canvas: the graph is up (close it and sample the 3D view after all), or the view is gone (an error fallback looks like this)
      if (!retry && (await js(`!!Array.from(document.querySelectorAll("button.gv-toggle")).find((b) => b.textContent === "Graph" && b.getAttribute("aria-pressed") === "true")`))) {
        events.graphClosedToSample = (events.graphClosedToSample ?? 0) + 1;
        await click("Graph"); await settle(600);
        return sample(where, true);
      }
      events.blank.push(`${where} (no 3D canvas, graph off)`);
      return;
    }
    events.samples += 1;
    events.minShare = Math.min(events.minShare ?? 1, s.share); events.lastShare = s.share;
    if (process.env.SOAK_DEBUG) console.log(`  sample ${where}: ${s.w}x${s.h}, ${s.colours} colours, not-background ${(s.share * 100).toFixed(2)}%${s.lost ? " CONTEXT LOST" : ""}`);
    if (s.lost || s.colours <= 1) events.blank.push(`${where} (${s.w}x${s.h}${s.lost ? ", context lost" : ", one colour"})`);
  };
  const memory = async () => {
    if (process.env.SOAK_GC) await js(`(async () => { for (let i = 0; i < 3; i++) { window.gc?.(); await new Promise((r) => setTimeout(r, 100)); } })()`); // SOAK_GC=1: read memory after forced collection
    const heap = await js(`Math.round((performance.memory?.usedJSHeapSize ?? 0) / 1048576)`).catch(() => -1);
    const procs = await app.evaluate(({ app: eapp }) => eapp.getAppMetrics().map((p) => [p.type, Math.round(p.memory.workingSetSize / 1024)]));
    const mb = (t) => procs.filter((p) => p[0] === t).reduce((a, p) => a + p[1], 0);
    return { heapMB: heap, rendererMB: mb("Tab"), gpuMB: mb("GPU") };
  };
  const open = async (robot) => {
    const calls = await app.evaluate((_, r) => { globalThis.__lwDialogPaths.robot = r; return globalThis.__lwDialogCalls; }, robot);
    if (await js(`!!document.querySelector(".open-face")`)) { await until("Open robot", `!!Array.from(document.querySelectorAll("button")).find((b) => b.textContent === "Open robot")`); await click("Open robot"); }
    else await app.evaluate(({ Menu }) => { const find = (items) => { for (const i of items) { if (i.label === "Open Robot…") return i; const s = i.submenu && find(i.submenu.items); if (s) return s; } }; find(Menu.getApplicationMenu().items).click(); });
    for (let i = 0; i < 100 && (await app.evaluate(() => globalThis.__lwDialogCalls)) === calls; i += 1) await settle(50);
    await settle(800);
    await until(`the check of ${robot}`, `(() => { const h = document.querySelector(".check-headline:not(.verdict-pending)"); return !!h && !document.querySelector(".open-face"); })()`);
    await settle(1500);
  };
  const menuRow = (label) => js(`(async () => { document.querySelector('.view-menu > button')?.click(); await new Promise((r) => setTimeout(r, 50)); const b = Array.from(document.querySelectorAll(".view-menu-list button")).find((b) => b.textContent.startsWith(${JSON.stringify(label)})); const ok = !!b && !b.disabled; if (ok) b.click(); if (document.querySelector(".view-menu-list")) document.querySelector('.view-menu > button')?.click(); return ok; })()`);
  const grab = (key) => js(`(() => { const c = Array.from(document.querySelectorAll("canvas")).find((c) => c.offsetWidth > 200); const gl = c?.getContext("webgl2"); if (!gl) return false; const px = new Uint8Array(gl.drawingBufferWidth * gl.drawingBufferHeight * 4); gl.readPixels(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight, gl.RGBA, gl.UNSIGNED_BYTE, px); window[${JSON.stringify(key)}] = px; return true; })()`);
  // "One colour" cannot see a missing robot over the drawn floor grid. Presence is differential: the same camera with
  // and without View > Show only <a device that has no mesh>; a drawn robot changes 0.25 to 1.5% of the view (by window and panel size), a missing one exactly 0.
  // Robots with no such device row are not measured (null). `expectAbsent` is the control.
  const presence = async (where, expectAbsent = false) => {
    await tab("Design"); await settle(500);
    const hasDevice = await js(`(() => { const b = Array.from(document.querySelectorAll("button.kit-tree-row")).find((b) => b.dataset.partId?.startsWith("dev.")); b?.click(); return !!b; })()`);
    if (!hasDevice) return null;
    await settle(900);
    if (!(await grab("__lwA"))) { events.blank.push(`${where} (no 3D canvas for the presence check)`); return null; }
    if (!expectAbsent && !(await menuRow("Show only selected part"))) return null; // the item is disabled for this row (drone): not measured
    await settle(800); await grab("__lwB");
    const changed = await js(`(() => { const a = window.__lwA, b = window.__lwB; if (a.length !== b.length) return 1; let n = 0, t = 0; for (let i = 0; i < a.length; i += 16) { t++; if (Math.abs(a[i] - b[i]) + Math.abs(a[i + 1] - b[i + 1]) + Math.abs(a[i + 2] - b[i + 2]) > 24) n++; } return n / t; })()`);
    await menuRow("Show everything"); await settle(500);
    (events.presence ??= []).push(changed);
    if (changed < 0.0005) events.blank.push(`${where} (robot not drawn: hiding it changed ${(changed * 100).toFixed(2)}% of the view)`);
    return changed;
  };
  await stubDialogs(app, { robot: firstRobot });
  await waitForSidecar(page);
  await js(hooks);
  page.on("load", () => { void js(hooks).catch(() => {}); });
  return { app, page, js, events, until, click, tab, size, harvest, sample, memory, open, hooks, menuRow, presence };
}

const say = (text) => ({ content: [{ type: "text", text }], stop_reason: "end_turn" });
const use = (name, input) => ({ content: [{ type: "tool_use", id: `t-${name}-${Math.random().toString(36).slice(2)}`, name, input }], stop_reason: "tool_use" });

/** One full cycle on the open robot. */
export async function cycle(d, name, card, n) {
  const { app, js, until, click, tab, size, sample } = d;
  await d.presence(`${name} #${n} after the switch`);
  for (const m of ["Design", "Check", "Plan", "Test", "Run"]) { await tab(m); await settle(500); await sample(`${name} #${n} ${m}`); }
  await tab("Check");
  if (await click("Graph")) { await settle(600); await click("Graph"); await settle(600); await sample(`${name} #${n} after graph`); }
  await tab("Design"); await settle(500);
  for (const lens of ["Power", "All"]) { await click(lens); await settle(400); await sample(`${name} #${n} lens ${lens}`); }
  // Every part once: open every closed group first, then click each row.
  await js(`document.querySelectorAll('.kit-tree-head button[aria-expanded="false"]').forEach((b) => b.click())`); await settle(300);
  const parts = await js(`document.querySelectorAll("button.kit-tree-row").length`);
  for (let i = 0; i < parts; i += 1) { await js(`document.querySelectorAll("button.kit-tree-row")[${i}]?.click()`); await settle(60); }
  await settle(300); await sample(`${name} #${n} after ${parts} parts`);
  await click("Fit view");
  for (let t = 0; t < 2; t += 1) { await js(`document.querySelector(".theme-toggle")?.click()`); await settle(400); await sample(`${name} #${n} theme ${t}`); }
  for (const [w, h] of [[1280, 800], [1920, 1080], [900, 700], [1280, 800]]) { await size(w, h); await settle(500); await sample(`${name} #${n} ${w}x${h}`); }
  // The Assistant: allow once per robot, one question for the findings rows, each eye then show all, one declare card applied.
  if (process.env.SOAK_NO_ASSISTANT) return; // 3D-only run: tells a 3D leak apart from the chat piling up
  if (!(await js(`!!document.querySelector('[aria-label="Ask about this robot"]')`))) {
    if (!(await js(`!!document.querySelector(".shell-drawer:not([hidden])")`)) || !(await js(`!!document.querySelector('.shell-drawer [aria-label="Ask about this robot"], .shell-drawer input[type="password"], .shell-drawer button')`))) await click("Assistant");
    await until("the Assistant", `!!document.querySelector('[aria-label="Ask about this robot"], .shell-drawer input[type="password"]') || !!Array.from(document.querySelectorAll(".shell-drawer button")).find((b) => /^Allow/.test(b.textContent.trim()))`, 20_000);
    if (await js(`!!document.querySelector('.shell-drawer input[type="password"]')`)) { await d.page.fill('.shell-drawer input[type="password"]', "sk-ant-made-up-for-soak"); await click("Save key and allow for this robot"); }
    else await js(`Array.from(document.querySelectorAll(".shell-drawer button")).find((b) => /^Allow/.test(b.textContent.trim()))?.click()`);
    await until("the question box", `!!document.querySelector('[aria-label="Ask about this robot"]')`, 20_000);
  }
  const ask = async (text, replies) => {
    const answers = await js(`document.querySelectorAll('.assistant-turn[data-who="assistant"]').length`);
    await app.evaluate((_, r) => { globalThis.__lwReplies.push(...r); }, replies);
    await d.page.fill('[aria-label="Ask about this robot"]', text);
    await d.page.press('[aria-label="Ask about this robot"]', "Enter");
    await until(`the answer to "${text}"`, `document.querySelectorAll('.assistant-turn[data-who="assistant"]').length === ${answers + 1}`);
    await app.evaluate(() => { globalThis.__lwReplies.length = 0; });
  };
  await ask("What does the check find?", [use("check", {}), say("These are the check's findings.")]);
  const eyes = await js(`(() => { const l = document.querySelectorAll(".assistant-findings"); return l.length ? l[l.length - 1].querySelectorAll("button").length : 0; })()`);
  for (let i = 0; i < eyes; i += 1) {
    const shows = await app.evaluate(() => globalThis.__lwShows);
    await js(`(() => { const l = document.querySelectorAll(".assistant-findings"); l[l.length - 1].querySelectorAll("button")[${i}]?.click(); })()`);
    // The eye's show-only reaches the page only after main has read the robot's page from the engine (seconds on a busy
    // machine): wait until main has sent it before show all, or show all can arrive first (3 of 215 in the final run).
    for (let t = 0; t < 150 && (await app.evaluate(() => globalThis.__lwShows)) === shows; t += 1) await settle(100);
    if ((await app.evaluate(() => globalThis.__lwShows)) === shows) d.events.driver.push(`${name} #${n} eye ${i + 1}: no show message from main in 15 s`);
    await settle(400); await sample(`${name} #${n} eye ${i + 1}/${eyes}`);
    await app.evaluate(({ BrowserWindow }, c) => BrowserWindow.getAllWindows()[0].webContents.send("lockwire:assistant-show", { contract: c, action: "show-all" }), d.contractPath);
    await settle(300);
    if (await js(`!!Array.from(document.querySelectorAll("button.gv-toggle")).find((b) => b.textContent === "Only what this check uses" && b.getAttribute("aria-pressed") === "true")`)) d.events.showAllMissed.push(`${name} #${n} eye ${i + 1}`);
    // A finding with no drawn parts opens the graph; close it so the 3D view is what is sampled (the older blank-after-finding bug).
    if (await js(`!!Array.from(document.querySelectorAll("button.gv-toggle")).find((b) => b.textContent === "Graph" && b.getAttribute("aria-pressed") === "true")`)) { await click("Graph"); d.events.graphAfterEye = (d.events.graphAfterEye ?? 0) + 1; }
    await settle(400); await sample(`${name} #${n} show all after eye ${i + 1}`);
  }
  d.events.eyes = (d.events.eyes ?? 0) + eyes;
  if (eyes) await d.presence(`${name} #${n} after the eyes and show all`);
  if (card) {
    const value = n % 2 ? card.value : Number((card.value * 1.01).toPrecision(6));
    await ask("Record this value.", [use("declare", { path: card.path, value: `${value} ${card.unit}`, source: "3D soak, scripted" }), say("This card records it.")]);
    const applied = await click("Apply");
    if (applied) {
      await settle(300);
      if (await js(`!!document.querySelector(".assistant-name input")`)) { await d.page.fill(".assistant-name input", "Soak"); await click("Save"); }
      await until("Applied", `Array.from(document.querySelectorAll(".assistant-card")).at(-1)?.textContent.includes("Applied")`, 60_000);
      await tab("Check");
      await until("the re-check", `!!document.querySelector(".check-headline:not(.verdict-pending)")`);
      await settle(1200); await sample(`${name} #${n} after apply`);
      await d.presence(`${name} #${n} after apply`);
      d.events.applied = (d.events.applied ?? 0) + 1;
    } else d.events.driver.push(`${name} #${n}: no Apply button on the declare card (${(await js(`Array.from(document.querySelectorAll(".assistant-card")).at(-1)?.textContent ?? "no card"`)).slice(0, 200)})`);
  }
}

/** Hide every part (each top-level row: hiding flips a row's whole subtree, so a child row would show it again), then
 * View > Show everything; only the restored view is judged. */
async function hideAll(d, name) {
  const { js, click, sample } = d;
  await d.tab("Design"); await settle(500);
  await js(`document.querySelectorAll('.kit-tree-head button[aria-expanded="false"]').forEach((b) => b.click())`); await settle(300);
  const top = `Array.from(document.querySelectorAll("button.kit-tree-row")).filter((b) => !b.style.paddingLeft)`;
  const rows = await js(`${top}.length`);
  const menuRow = (label) => js(`(async () => { document.querySelector('.view-menu > button')?.click(); await new Promise((r) => setTimeout(r, 50)); const b = Array.from(document.querySelectorAll(".view-menu-list button")).find((b) => b.textContent.startsWith(${JSON.stringify(label)})); const ok = !!b && !b.disabled; if (ok) b.click(); if (document.querySelector(".view-menu-list")) document.querySelector('.view-menu > button')?.click(); return ok; })()`);
  let hidden = 0;
  for (let i = 0; i < rows; i += 1) { await js(`${top}[${i}]?.click()`); await settle(60); if (await menuRow("Hide selected part")) hidden += 1; }
  await settle(500);
  const before = d.events.blank.length;
  await sample(`${name} every part hidden`);
  const emptyWhenHidden = d.events.blank.length > before ? "yes" : `no, ${(d.events.lastShare * 100).toFixed(1)}% still drawn`; // not an event either way
  d.events.blank.length = before;
  const reset = await menuRow("Show everything");
  await click("Fit view"); await settle(600);
  await sample(`${name} show everything after hiding all`);
  (d.events.hideAll ??= []).push(`${name}: ${hidden}/${rows} rows hidden, view empty while hidden: ${emptyWhenHidden}, Show everything pressed: ${reset}`);
}

/** Item 3's risky inputs, each a copy in the temp folder: arm-power with one thing broken, and the arm without its scene file. */
function riskyRobots(tmp) {
  const base = copyStarters(tmp, ["arm-power", "arm"]);
  const ap = readFileSync(base["arm-power"], "utf8");
  const swap = (text, from, to) => { if (!text.includes(from)) throw new Error(`risky input: arm-power no longer has ${from}`); return text.replace(from, to); };
  const variant = (name, text, files = {}) => {
    const dir = join(tmp, name);
    mkdirSync(dir);
    writeFileSync(join(dir, `${name}.yaml`), text);
    for (const [f, bytes] of Object.entries(files)) writeFileSync(join(dir, f), bytes);
    return join(dir, `${name}.yaml`);
  };
  const TIP = "    - kind: box  # design choice: 20 mm x 10 mm x 10 mm fingertip block at the forearm end\n      size: [0.02, 0.01, 0.01]";
  const BASE = "    - kind: box\n      size: [0.12, 0.12, 0.01]";
  const extraLinks = Array.from({ length: 300 }, (_, i) => `  - name: extra-${i}\n    geometry:\n    - kind: box\n      size: [0.004, 0.004, 0.004]\n`).join("");
  const extraJoints = Array.from({ length: 300 }, (_, i) => `  - name: extra-j${i}\n    type: fixed\n    parent: base\n    child: extra-${i}\n    origin:\n      xyz: [${((i % 20) * 0.006 - 0.057).toFixed(3)}, ${(Math.floor(i / 20) * 0.006 - 0.042).toFixed(3)}, 0.012]\n`).join("");
  // The arm ships a glTF scene: three broken copies of it reach the 3D view's own mesh path (arm-power has no scene file).
  const glbOf = (json) => { const j = Buffer.from(JSON.stringify(json).padEnd(Math.ceil(JSON.stringify(json).length / 4) * 4, " ")); const head = Buffer.alloc(20); head.write("glTF", 0); head.writeUInt32LE(2, 4); head.writeUInt32LE(20 + j.length, 8); head.writeUInt32LE(j.length, 12); head.write("JSON", 16); return Buffer.concat([head, j]); };
  const armVariant = (name, glb) => { const dir = join(tmp, name); mkdirSync(dir); copyFileSync(base.arm, join(dir, `${name}.yaml`)); writeFileSync(join(dir, `${name}.glb`), glb); return join(dir, `${name}.yaml`); };
  const glb = readFileSync(join(tmp, "arm", "arm.glb"));
  const armTruncated = armVariant("arm-glb-truncated", glb.subarray(0, Math.floor(glb.length / 2)));
  const armNoMeshes = armVariant("arm-glb-no-meshes", glbOf({ asset: { version: "2.0" }, scenes: [{ nodes: [] }], scene: 0 }));
  const armZeroTriangles = armVariant("arm-glb-zero-triangles", glbOf({ asset: { version: "2.0" }, scenes: [{ nodes: [0] }], scene: 0, nodes: [{ name: "base_link", mesh: 0 }], meshes: [{ primitives: [{ attributes: { POSITION: 0 } }] }], accessors: [{ componentType: 5126, count: 0, type: "VEC3", min: [0, 0, 0], max: [0, 0, 0] }] }));
  rmSync(join(tmp, "arm", "arm.glb"));
  return {
    "arm-glb-truncated": armTruncated,
    "arm-glb-no-meshes": armNoMeshes,
    "arm-glb-zero-triangles": armZeroTriangles,
    "mesh-missing": variant("mesh-missing", swap(ap, TIP, "    - kind: mesh\n      mesh: missing.stl")),
    "mesh-zero-triangles": variant("mesh-zero-triangles", swap(ap, TIP, "    - kind: mesh\n      mesh: empty.stl"), { "empty.stl": Buffer.alloc(84) }), // binary STL: 80-byte header, 0 triangles
    "size-huge": variant("size-huge", swap(ap, BASE, "    - kind: box\n      size: [1000, 1000, 1000]")),
    "size-zero": variant("size-zero", swap(ap, BASE, "    - kind: box\n      size: [0, 0, 0]")),
    "parts-300": variant("parts-300", swap(swap(ap, "  joints:\n  - name: shoulder", `  joints:\n${extraJoints}  - name: shoulder`), "  joints:\n", `${extraLinks}  joints:\n`)),
    "arm-scene-missing": base.arm,
    "arm-power-hide-all": base["arm-power"],
  };
}

async function main() {
  const tmp = mkdtempSync(join(tmpdir(), "lw-soak-"));
  const risky = roundsArg === "risky";
  const robots = risky ? riskyRobots(tmp) : copyStarters(tmp, only.length ? only : [...DEMO, ...OTHERS]);
  const names = Object.keys(robots);
  const cards = Object.fromEntries(names.map((n) => { try { return [n, declarable(robots[n])]; } catch { return [n, null]; } }));
  const t0 = Date.now();
  process.env.TMPDIR = tmp; // launchApp's own temp folders land here and go with it
  let controls = null;
  const d = await startSoakApp(tmp, robots[names[0]]);
  const perRobot = Object.fromEntries(names.map((n) => [n, { cycles: 0, mem: [] }]));
  const log = (s) => console.log(`[${((Date.now() - t0) / 60000).toFixed(1)} min] ${s}`);
  log(`hidden: window visible ${(await d.app.evaluate(() => globalThis.__soak.visible))[0]}, dock visible ${(await d.app.evaluate(() => globalThis.__soak.visible))[1]}`);
  try {
    // The counters' own proof, once: a forced WebGL context loss and restore must be counted and the robot drawn again.
    const controlRobot = robots["arm-power-hide-all"] ?? robots[names[0]]; // a robot that opens (the risky ones may not)
    d.contractPath = controlRobot;
    await d.open(controlRobot);
    await d.js(`window.__lwLose = Array.from(document.querySelectorAll("canvas")).find((c) => c.offsetWidth > 200).getContext("webgl2").getExtension("WEBGL_lose_context"); window.__lwLose.loseContext()`);
    await settle(1000); await d.js(`window.__lwLose.restoreContext()`); await settle(1500);
    const blankBefore = d.events.blank.length;
    await d.sample("control: restored context, untouched");
    await d.harvest();
    controls = { lostCounted: d.events.contextLost, restoredCounted: d.events.contextRestored, redrawnAfterRestore: d.events.blank.length === blankBefore };
    // The presence check must fail on a view with the robot hidden, and pass on the drawn one.
    const drawn = await d.presence("control: robot drawn");
    await d.tab("Design"); await d.js(`Array.from(document.querySelectorAll("button.kit-tree-row")).find((b) => b.dataset.partId?.startsWith("dev."))?.click()`); await settle(900);
    await d.menuRow("Show only selected part"); await settle(800);
    const before2 = d.events.blank.length;
    await d.presence("control: robot hidden", true);
    controls.presenceDrawn = drawn; controls.presenceCatchesHidden = d.events.blank.length > before2;
    // Every other counter fires once: a console error, a renderer throw, a renderer rejection, a main-process throw,
    // and our own app's GPU process killed (a real GPU restart: the view must come back drawn).
    await d.js(`console.error("soak control"); setTimeout(() => { throw new Error("soak control"); }); Promise.reject(new Error("soak control")); 1`);
    await d.app.evaluate(() => { setTimeout(() => { throw new Error("soak control"); }); });
    const gpu = await d.app.evaluate(({ app: eapp }) => { const g = eapp.getAppMetrics().find((p) => p.type === "GPU"); if (g) process.kill(g.pid); return g?.pid ?? null; });
    await settle(5000);
    await d.harvest();
    const e0 = d.events;
    Object.assign(controls, { consoleErrorCounted: e0.consoleErrors.length > 0, rendererThrowCounted: e0.pageErrors.length > 0 && e0.rendererUncaught.length > 0, rejectionCounted: e0.rendererRejections.length > 0, mainThrowCounted: e0.mainUncaught.length > 0, gpuKilled: gpu !== null, gpuGoneCounted: e0.childGone.some((c) => c.includes("GPU")), contextLostAfterGpuKill: e0.contextLost > controls.lostCounted });
    const before3 = d.events.blank.length;
    controls.drawnAfterGpuRestart = (await d.presence("control: after the GPU process restart")) !== null && d.events.blank.length === before3;
    for (const k of ["blank", "consoleErrors", "pageErrors", "rendererUncaught", "rendererRejections", "mainUncaught", "childGone", "presence"]) d.events[k] = [];
    d.events.contextLost = 0; d.events.contextRestored = 0;
    log(`control: ${JSON.stringify(controls)}`);
    for (let round = 1; round <= ROUNDS; round += 1) {
      for (const name of names) {
        if (!risky && !DEMO.includes(name) && round > OTHER_ROUNDS) continue;
        const before = d.events.blank.length, samplesBefore = d.events.samples;
        try {
          d.contractPath = robots[name];
          await d.open(robots[name]);
          const start = await d.memory();
          await cycle(d, name, cards[name], round);
          if (risky) await hideAll(d, name);
          const end = await d.memory();
          perRobot[name].cycles += 1; perRobot[name].mem.push([start, end]);
          await d.harvest();
          log(`${name} round ${round}: heap ${start.heapMB}->${end.heapMB} MB, renderer ${start.rendererMB}->${end.rendererMB} MB, gpu ${start.gpuMB}->${end.gpuMB} MB, blank +${d.events.blank.length - before}, canvas samples ${d.events.samples - samplesBefore}`);
        } catch (e) {
          const shown = await d.js(`(document.querySelector(".open-face-error, .failure-banner, [role=alert], .kit-notice")?.textContent ?? "").slice(0, 300)`).catch(() => "");
          d.events.driver.push(`${name} round ${round}: ${String(e).slice(0, 300)}${shown ? ` (the page says: ${shown})` : ""}`);
          log(`${name} round ${round}: DRIVER ERROR ${String(e).slice(0, 300)}`);
          await d.harvest().catch(() => {});
          if (await d.js("1").catch(() => null) !== 1) { await d.app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.reload()); await settle(3000); }
        }
      }
    }
  } finally {
    await d.harvest().catch(() => {});
    await d.app.close().catch(() => {});
    rmSync(tmp, { recursive: true, force: true });
  }
  const e = d.events;
  const summary = {
    minutes: Number(((Date.now() - t0) / 60000).toFixed(1)), controls,
    cycles: Object.fromEntries(Object.entries(perRobot).map(([n, r]) => [n, r.cycles])),
    memory: Object.fromEntries(Object.entries(perRobot).filter(([, r]) => r.mem.length).map(([n, r]) => [n, { first: r.mem[0], last: r.mem.at(-1) }])),
    rendererGone: e.rendererGone, childGone: e.childGone, contextLost: e.contextLost, contextRestored: e.contextRestored,
    rendererUncaught: e.rendererUncaught, rendererRejections: e.rendererRejections, pageErrors: e.pageErrors, mainUncaught: e.mainUncaught,
    consoleErrors: e.consoleErrors.length, consoleErrorSamples: [...new Set(e.consoleErrors)].slice(0, 10),
    blankSamples: e.blank.length, blank: e.blank.slice(0, 20), canvasSamples: e.samples, presenceChecks: (e.presence ?? []).length, smallestPresence: (e.presence ?? []).length ? Math.min(...e.presence) : null, graphClosedToSample: e.graphClosedToSample ?? 0, eyesPressed: e.eyes ?? 0, graphOpenedByEye: e.graphAfterEye ?? 0, cardsApplied: e.applied ?? 0, hideAll: e.hideAll ?? [], showAllMissed: e.showAllMissed, nativeDialogs: e.nativeDialogs, windowSizes: [...new Set(e.sizes)], driverErrors: e.driver,
  };
  console.log(JSON.stringify(summary, null, 1));
  const bad = e.rendererGone.length + e.childGone.length + e.contextLost + e.rendererUncaught.length + e.rendererRejections.length + e.pageErrors.length + e.mainUncaught.length + e.blank.length + e.driver.length + e.consoleErrors.length + e.nativeDialogs.length + e.showAllMissed.length
    + (controls && Object.entries(controls).every(([k, v]) => k === "presenceDrawn" || v) ? 0 : 1);
  process.exitCode = bad ? 1 : 0;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
