// Focus-ring audit on the REAL app: walks the screens, gives every focusable control the keyboard
// focus, and measures where its ring is drawn. A ring must sit inside its own control: one drawn
// outside is cut off by any panel, list or sheet that scrolls or has rounded corners (the "blue
// highlight spills out of the box" defect). Exits 1 when any control breaks that, or has no ring.
//
// usage: node scripts/focus-audit.mjs [dark|light] [--json out.json]     (run `npm run build` first)
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { launchApp, waitForSidecar } from "./e2e-drive.mjs";

const APP = join(dirname(fileURLToPath(import.meta.url)), "..");
const theme = process.argv[2] === "light" ? "light" : "dark";
const jsonOut = process.argv.includes("--json") ? process.argv[process.argv.indexOf("--json") + 1] : null;
const settle = (ms) => new Promise((r) => setTimeout(r, ms));

// Runs in the page. Returns one row per control whose ring is wrong.
const MEASURE = `(() => {
  const px = (v) => parseFloat(v) || 0;
  const name = (e) => e.tagName.toLowerCase() + (typeof e.className === "string" && e.className.trim() ? "." + e.className.trim().split(/\\s+/).slice(0, 3).join(".") : "");
  const shown = (e) => { const r = e.getBoundingClientRect(), c = getComputedStyle(e); return r.width > 0 && r.height > 0 && c.visibility !== "hidden" && !e.disabled && !e.closest("[inert]") && e.tabIndex >= 0 && r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight; }; // in the tab order and in the window
  const all = [...document.querySelectorAll('a[href], button, input, select, textarea, summary, [tabindex]:not([tabindex="-1"])')].filter(shown);
  const rows = [];
  let seen = 0;
  for (const el of all) {
    el.focus({ preventScroll: true });
    if (document.activeElement !== el) continue;
    seen++;
    // The ring is on the control, or on a wrapper that draws it for the control (a radio's label).
    let host = null, cs = null, glow = 0;
    for (let n = el, i = 0; n && i < 3; n = n.parentElement, i++) {
      const c = getComputedStyle(n);
      const outer = c.boxShadow === "none" ? [] : c.boxShadow.split(/,(?![^(]*\\))/).filter((s) => !/inset/.test(s) && /rgba?\\([^)]*\\)\\s+0px\\s+0px\\s+0px\\s+[1-9]/.test(s.trim()));
      if ((c.outlineStyle !== "none" && px(c.outlineWidth) > 0) || outer.length) { host = n; cs = c; glow = outer.length ? Math.max(...outer.map((s) => px(s.trim().split(/\\s+/).at(-1)))) : 0; break; }
    }
    const text = (el.getAttribute("aria-label") || el.textContent || el.getAttribute("placeholder") || "").trim().replace(/\\s+/g, " ").slice(0, 30);
    if (!host) { if (!el.matches(".cmdk-input")) rows.push({ what: "no ring", control: name(el), text }); continue; } // the command box shows its caret and a lit row instead
    const b = host.getBoundingClientRect();
    const hasOutline = cs.outlineStyle !== "none" && px(cs.outlineWidth) > 0;
    const grow = Math.max(hasOutline ? px(cs.outlineOffset) + px(cs.outlineWidth) : 0, glow);
    const ring = { l: b.left - grow, t: b.top - grow, r: b.right + grow, b: b.bottom + grow };
    const why = [];
    let scrolledOut = false;
    if (grow > 0.5) why.push("drawn " + grow.toFixed(0) + " px outside its control");
    for (let a = host.parentElement; a; a = a.parentElement) {
      const c = getComputedStyle(a);
      if (!/(hidden|auto|scroll|clip)/.test(c.overflowX + " " + c.overflowY)) continue;
      const r = a.getBoundingClientRect();
      const box = { l: r.left + px(c.borderLeftWidth), t: r.top + px(c.borderTopWidth), r: r.right - px(c.borderRightWidth), b: r.bottom - px(c.borderBottomWidth) };
      if (b.left < box.l - 0.5 || b.top < box.t - 0.5 || b.right > box.r + 0.5 || b.bottom > box.b + 0.5) { scrolledOut = true; break; } // scrolled partly out of this box: scrolling brings it back
      const cut = Math.max(box.l - ring.l, box.t - ring.t, ring.r - box.r, ring.b - box.b);
      if (cut > 0.5) { why.push("cut " + cut.toFixed(0) + " px by " + name(a)); break; }
      // A square-cornered ring in the rounded corner of the box that holds it.
      const R = px(c.borderTopLeftRadius), own = px(cs.borderTopLeftRadius) + Math.max(grow, 0);
      const inset = own * (1 - Math.SQRT1_2); // how far a rounded corner sits in from its square corner
      for (const [dx, dy] of [[ring.l - box.l, ring.t - box.t], [box.r - ring.r, ring.t - box.t], [ring.l - box.l, box.b - ring.b], [box.r - ring.r, box.b - ring.b]]) {
        const x = dx + inset, y = dy + inset;
        if (R > 1 && x < R && y < R && Math.hypot(R - x, R - y) > R + 0.75) { why.push("its corner pokes out of the rounded corner of " + name(a)); break; }
      }
      if (why.some((w) => w.startsWith("its corner"))) break;
    }
    if (!scrolledOut && (ring.l < -0.5 || ring.t < -0.5 || ring.r > innerWidth + 0.5 || ring.b > innerHeight + 0.5)) why.push("runs off the window"); // a row scrolled under its list's edge is not off the window
    if (why.length) rows.push({ what: why.join("; "), control: name(el), text });
  }
  document.activeElement?.blur?.();
  return { seen, rows };
})()`;

const tmp = mkdtempSync(join(tmpdir(), "lw-focus-audit-"));
writeFileSync(join(tmp, "config.toml"), 'witness = "Focus Audit"\n');
const { app, page } = await launchApp({
  args: [".", "--user-data-dir=" + join(tmp, "ud")], cwd: APP,
  env: { LOCKWIRE_WITNESS_CONFIG: join(tmp, "config.toml"), LOCKWIRE_SIDECAR_COMMAND: "uv", LOCKWIRE_SIDECAR_ARGS: "run --project " + join(APP, "..") + " lockwire serve --exit-on-stdin-close" },
});
const js = (s) => page.evaluate(s);
const click = (re, sel = "button, [role=tab], [role=menuitem], summary") =>
  js(`(() => { const e = Array.from(document.querySelectorAll(${JSON.stringify(sel)})).find((b) => ${re}.test((b.textContent || "").trim())); e?.click(); return !!e; })()`);
const report = [];
let total = 0;
const measure = async (screen) => {
  await page.keyboard.press("Shift"); // keyboard was the last input, so a scripted focus shows the keyboard ring
  const { seen, rows } = await js(MEASURE);
  total += seen;
  for (const r of rows) report.push({ screen, ...r });
  console.log(`${screen}: ${seen} controls, ${rows.length} wrong`);
};
try {
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1440, 900));
  await js(`localStorage.setItem("lockwire-theme", "${theme}")`);
  await page.reload(); await page.waitForLoadState("load"); await waitForSidecar(page); await settle(1800);
  await page.keyboard.press("Tab");
  await measure("Start");
  await click("/desk arm/i", ".open-face-new li button"); await settle(9000);
  await measure("Check");
  await js('document.querySelector(".kit-finding")?.click()'); await settle(900);
  if (!(await js('!!document.querySelector(".check-detail")'))) { await js('document.querySelector(".kit-finding")?.click()'); await settle(900); }
  await js('document.querySelectorAll(".check-detail summary").forEach((s) => s.click())'); await settle(500);
  await measure("Check, finding open");
  await click("/^Design$/", "[role=tab]"); await settle(2500);
  await measure("Design");
  await js('(() => { const r = Array.from(document.querySelectorAll(".kit-tree-row")).find((r) => !r.classList.contains("kit-tree-head") && /Upper Arm/.test(r.textContent)); r?.click(); })()'); await settle(1500);
  await js('document.querySelectorAll(".kit-slot-right summary").forEach((s) => s.click())'); await settle(700);
  await measure("Design, part selected, folds open");
  await click("/^Add part$/", "button"); await settle(1200);
  await measure("Design, add part");
  await page.keyboard.press("Escape"); await settle(300);
  for (const m of ["Plan", "Test", "Run"]) { await click(`/^${m}$/`, "[role=tab]"); await settle(2200); await measure(m); }
  // More is clicked by its class: its label turns into the open screen's name once one is open.
  for (const m of ["Declare", "Docs"]) { await js('document.querySelector(".shell-more")?.click()'); await settle(400); await measure("More menu"); await click(`/^${m}$/`, "[role=menuitem], button"); await settle(2200); await measure(m); }
  await click("/^Check$/", "[role=tab]"); await settle(1500);
  await click("/^View/"); await settle(400); await measure("View menu"); await page.keyboard.press("Escape");
  await click("/^Panels$/"); await settle(1800);
  const tabs = await js('Array.from(document.querySelectorAll(".shell-drawer [role=tab]")).map((t) => t.textContent.trim())');
  for (const [i, t] of tabs.entries()) { await js(`document.querySelectorAll(".shell-drawer [role=tab]")[${i}]?.click()`); await settle(1500); await measure("Drawer: " + t); }
  await click("/^Assistant$/"); await settle(1500); await measure("Assistant");
  await page.keyboard.press("Escape"); await page.keyboard.press("Meta+k"); await settle(500); await measure("Command box");
} catch (e) {
  console.log("ERR", String(e).slice(0, 300));
  report.push({ screen: "(audit)", what: "the audit itself failed: " + String(e).slice(0, 120), control: "", text: "" });
} finally {
  await app.close().catch(() => {});
  rmSync(tmp, { recursive: true, force: true });
}
const kinds = new Map();
for (const r of report) { const k = r.control + " | " + r.what.replace(/\d+ px/g, "N px"); kinds.set(k, [...(kinds.get(k) ?? []), r]); }
console.log(`\n${theme}: ${total} controls measured, ${report.length} wrong, ${kinds.size} distinct`);
for (const [k, rs] of [...kinds].sort((a, b) => b[1].length - a[1].length)) console.log(`  ${String(rs.length).padStart(3)}x ${k}   e.g. [${rs[0].screen}] "${rs[0].text}"`);
if (jsonOut) writeFileSync(jsonOut, JSON.stringify({ theme, total, report }, null, 1));
process.exit(report.length ? 1 : 0);
