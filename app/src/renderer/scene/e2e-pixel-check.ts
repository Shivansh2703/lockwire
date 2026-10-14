/**
 * Runs the REAL scene graph (`buildScene`) inside a hidden Electron `BrowserWindow` and reads
 * back one pixel with `gl.readPixels` — the only way to prove a mesh was actually drawn rather
 * than merely that a component mounted (never a screenshot, never a window on the
 * owner's screen). `scene-pixel.e2e.test.ts` bundles this file with esbuild for the renderer
 * side, spawns the real electron binary, and reads the JSON line
 * this script writes to stdout — the same shape as e2-i2's `hidden-launch-check.ts`.
 */
import { app, BrowserWindow } from "electron";

import { harnessWindowOptions } from "../../main/window-options";

// The HTML path is passed via env, not resolved from import.meta.url/__dirname: this file is
// esbuild-bundled to CommonJS to run under electron (same idiom as e2-i2's
// hidden-launch-check.ts), and neither of those resolves the same way after bundling.
// This window is always hidden (`harnessWindowOptions(true)`). Width/height/webPreferences stay
// bespoke (64x64, no preload): this window loads a bare check page, not the production app, so
// windowOptions() itself (1100x750, app preload, sandbox) does not fit. e2-hh3: the bespoke shape
// is now its own pure function, harnessWindowOptions() in window-options.ts, so the `show` flag's
// two directions are provable on the returned object instead of by a live launch.
void app.whenReady().then(async () => {
  app.dock?.hide();
  const win = new BrowserWindow(harnessWindowOptions(true));

  const htmlPath = process.env.LOCKWIRE_E2E_HTML_PATH;
  if (!htmlPath) throw new Error("LOCKWIRE_E2E_HTML_PATH not set");
  await win.loadFile(htmlPath);

  const partsJson = process.env.LOCKWIRE_E2E_PARTS ?? "[]";
  const pixel = (await win.webContents.executeJavaScript(
    `window.__runSceneCheck(${JSON.stringify(partsJson)})`,
  )) as [number, number, number, number];

  const result = { visible: win.isVisible(), pixel };
  process.stdout.write(`${JSON.stringify(result)}\n`);
  app.quit();
});
