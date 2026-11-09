// README screenshots from the FREE app: node scripts/readme-shots.mjs <built-free-app-dir> <out-dir>
// Drives Open -> Or try an example -> Rover -> Check (GO with notes), light theme, 1280x800. Frames land in <out-dir>.
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { launchApp, waitForSidecar } from "./e2e-drive.mjs";

const [appDir, outDir] = process.argv.slice(2);
mkdirSync(outDir, { recursive: true });
const robotDir = "/tmp/my-robot";
mkdirSync(robotDir, { recursive: true });
// Free sidecar (set LOCKWIRE_FREE_SIDECAR to a script that runs `lockwire` with the paid plugin blocked)
// and a fresh user-data dir, so no recent files from the operator's machine show up.
const { app, page } = await launchApp({
  args: [".", `--user-data-dir=${mkdtempSync(join(tmpdir(), "lw-shots-"))}`], cwd: appDir,
  env: { LOCKWIRE_SIDECAR_COMMAND: process.env.LOCKWIRE_FREE_SIDECAR, LOCKWIRE_SIDECAR_ARGS: "serve --exit-on-stdin-close" },
});
await app.evaluate(({ BrowserWindow, dialog }, out) => {
  const w = BrowserWindow.getAllWindows()[0];
  w.setContentSize(1280, 800);
  dialog.showSaveDialog = async () => ({ canceled: false, filePath: out });
}, join(robotDir, "lockwire.yaml"));
await page.evaluate(`localStorage.setItem("lockwire-theme","light")`);
await page.reload();
await page.waitForLoadState("load");
await waitForSidecar(page);
const settle = (ms) => new Promise((r) => setTimeout(r, ms));
const shot = async (name) => {
  const bytes = Buffer.from(await app.evaluate(async ({ BrowserWindow }) =>
    Array.from((await BrowserWindow.getAllWindows()[0].webContents.capturePage()).toPNG())));
  writeFileSync(join(outDir, name + ".png"), bytes);
};
await settle(800);
await shot("f0-open");
await page.evaluate(`Array.from(document.querySelectorAll("button")).find((b) => b.textContent.startsWith("Rover"))?.click();`);
await page.waitForSelector(".check-headline", { timeout: 30000 });
await page.evaluate(`document.querySelector(".check-list > .kit-disclosure summary")?.click()`);
await settle(1500);
await shot("f1-check");
console.log(await page.evaluate(`document.querySelector(".check-headline").textContent`));
await app.close();
