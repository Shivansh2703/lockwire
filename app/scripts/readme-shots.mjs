// README pictures from the FREE app, hidden: node scripts/readme-shots.mjs <built-free-app-dir> <out-dir>
//   sheet-<name>.png  the nine bundled examples, dark, 1280x800, Check screen (one app launch each)
//   gif-0..2.png      Open -> example -> Rover -> Check, light, 1280x800 (the frames of try-example.gif)
// LOCKWIRE_FREE_SIDECAR: a script that runs `lockwire` with the paid plugin blocked. No model call is made.
// A frame is kept only after the 3D view has drawn the robot and stopped changing (a blank canvas is never shot).
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { launchApp, waitForSidecar } from "./e2e-drive.mjs";

const [appDir, outDir] = process.argv.slice(2);
mkdirSync(outDir, { recursive: true });
const settle = (ms) => new Promise((r) => setTimeout(r, ms));
const EXAMPLES = [["arm-power", "Desk arm"], ["servo-bench", "Servo bench"], ["arm", "Arm ·"], ["drone", "Drone"], ["humanoid", "Humanoid"],
  ["quadruped", "Quadruped"], ["rover", "Rover · TurtleBot3"], ["hexapod-param", "Hexapod"], ["rover-param", "Rover · resizable"]];

async function launch(theme) {
  const { app, page } = await launchApp({
    args: [".", `--user-data-dir=${mkdtempSync(join(tmpdir(), "lw-shots-"))}`], cwd: appDir,
    env: { LOCKWIRE_SIDECAR_COMMAND: process.env.LOCKWIRE_FREE_SIDECAR, LOCKWIRE_SIDECAR_ARGS: "serve --exit-on-stdin-close" },
  });
  await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].setContentSize(1280, 800));
  await page.evaluate(`localStorage.setItem("lockwire-theme","${theme}")`);
  await page.reload();
  await page.waitForLoadState("load");
  await waitForSidecar(page);
  await settle(800);
  const hidden = !(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].isVisible()));
  if (!hidden) throw new Error("a window is visible: stopped");
  const bitmap = () => app.evaluate(async ({ BrowserWindow }) => Array.from((await BrowserWindow.getAllWindows()[0].webContents.capturePage()).toPNG()));
  /** Pixels in the 3D area (left 45%, below the title) that differ from the corner colour: the robot. */
  const robotPixels = () => app.evaluate(async ({ BrowserWindow }) => {
    const img = await BrowserWindow.getAllWindows()[0].webContents.capturePage();
    const { width, height } = img.getSize(), b = img.toBitmap();
    const lum = (x, y) => { const i = (y * width + x) * 4; return 0.114 * b[i] + 0.587 * b[i + 1] + 0.299 * b[i + 2]; };
    const bg = lum(Math.floor(width * 0.02), Math.floor(height * 0.5));
    let n = 0;
    for (let y = Math.floor(height * 0.14); y < height * 0.86; y += 2) for (let x = Math.floor(width * 0.03); x < width * 0.42; x += 2) if (Math.abs(lum(x, y) - bg) > 30) n += 1;
    return n;
  });
  const drawn = async (min = 600) => {
    let last = -1;
    for (let i = 0; i < 80; i += 1) {
      const n = await robotPixels();
      if (n >= min && last >= min && Math.abs(n - last) <= last * 0.02) return n;
      last = n; await settle(700);
    }
    throw new Error("the 3D view never settled on a drawn robot");
  };
  /** What the frame shows, from the page: the count line, the findings and each list's rows, and the three must-nots. */
  const facts = () => page.evaluate(`(() => {
    const text = document.body.innerText, list = document.querySelector(".check-list");
    const lists = list ? Array.from(list.querySelectorAll(".kit-disclosure")).map((d) => ({ title: d.querySelector("summary")?.textContent.trim(), open: d.open,
      rows: d.open ? Array.from(d.querySelectorAll(".kit-finding")).map((r) => r.textContent.trim()) : null })) : [];
    return { count: document.querySelector(".check-headline")?.textContent ?? null, findings: list ? Array.from(list.children).filter((c) => c.querySelector(".kit-finding")).map((c) => c.textContent.trim().slice(0, 80)) : [], lists,
      w0: /\\bw0\\b/.test(text), longDash: /[\u2013\u2014]/.test(text),
      paidTab: Array.from(document.querySelectorAll("button, [role=tab]")).some((b) => /^(Test|Run)$/.test(b.textContent.trim())) };
  })()`);
  const shot = async (name) => { writeFileSync(join(outDir, name + ".png"), Buffer.from(await bitmap())); console.log(name, JSON.stringify(await facts())); };
  const click = (prefix) => page.evaluate(`(() => { const b = Array.from(document.querySelectorAll("button")).find((b) => b.textContent.trim().startsWith(${JSON.stringify(prefix)})); b?.click(); return !!b; })()`);
  const check = async () => { await page.waitForSelector(".check-headline:not(.verdict-pending)", { timeout: 60000 }); };
  return { app, page, shot, click, check, drawn, settle };
}

const mode = process.env.SHOTS ?? "all";
if (mode !== "gif") for (const [name, label] of EXAMPLES) {
  const d = await launch("dark");
  if (!(await d.click(label))) throw new Error("no example button: " + label);
  await d.check();
  await d.settle(2000);
  // fold the first finding the screen opens by itself, so every list the count line names fits the tile
  await d.page.evaluate(`document.querySelector(".check-list .kit-finding.is-selected")?.click()`);
  await d.settle(1000);
  const px = await d.drawn(name === "drone" || name === "hexapod-param" || name === "rover-param" ? 300 : 600);
  await d.shot("sheet-" + name);
  console.log(name, "robot px", px, "|", (await d.page.evaluate(`document.querySelector(".check-headline").textContent`)));
  await d.app.close();
}
if (mode !== "sheet") {
  const d = await launch("light");
  await d.shot("gif-0");
  await d.click("Rover · TurtleBot3");
  await d.check();
  await d.settle(1500);
  await d.drawn();
  await d.shot("gif-1");
  await d.page.evaluate(`document.querySelector(".check-list > .kit-disclosure summary")?.click()`);
  await d.settle(500);
  // the opened list's last row into view, so every not-scored row the count names is on the frame
  console.log("gif-2 scrolled", await d.page.evaluate(`(() => { let e = document.querySelector(".check-list");
    while (e && !(e.scrollHeight > e.clientHeight + 4 && /auto|scroll/.test(getComputedStyle(e).overflowY))) e = e.parentElement;
    if (e) e.scrollTop = e.scrollHeight; return e ? e.className + " " + e.scrollTop : "no scroller"; })()`));
  await d.settle(1500);
  await d.drawn();
  await d.shot("gif-2");
  console.log("gif", await d.page.evaluate(`document.querySelector(".check-headline").textContent`));
  await d.app.close();
}
