/**
 * Runs the REAL `Canvas` + `DesignLens` mount inside a hidden Electron `BrowserWindow`, clicks a
 * real lens tab, and reads back the framebuffer with `gl.readPixels` — the only way to prove a
 * lens actually changed what is DRAWN rather than merely which state variable holds its id
 * (never a screenshot, never a window on the owner's screen). `lenses.e2e.test.ts`
 * bundles this file with esbuild for the renderer side, spawns the real electron binary,
 * and reads the JSON line this script writes to stdout — same idiom as
 * `e2e-pixel-check.ts`.
 */
import { app, BrowserWindow } from "electron";

void app.whenReady().then(async () => {
  app.dock?.hide();
  const win = new BrowserWindow({
    show: false,
    width: 64,
    height: 64,
    webPreferences: { offscreen: false },
  });

  const htmlPath = process.env.LOCKWIRE_E2E_HTML_PATH;
  if (!htmlPath) throw new Error("LOCKWIRE_E2E_HTML_PATH not set");
  await win.loadFile(htmlPath);

  const partsJson = process.env.LOCKWIRE_E2E_PARTS ?? "[]";
  const graphJson = process.env.LOCKWIRE_E2E_GRAPH;
  if (!graphJson) throw new Error("LOCKWIRE_E2E_GRAPH not set");

  await win.webContents.executeJavaScript(
    `window.__mount(${JSON.stringify(partsJson)}, ${JSON.stringify(graphJson)})`,
  );

  const beforePixel = (await win.webContents.executeJavaScript(
    `window.__readCenterPixel()`,
  )) as [number, number, number, number];

  const selectLens = process.env.LOCKWIRE_E2E_SELECT_LENS;
  if (!selectLens) throw new Error("LOCKWIRE_E2E_SELECT_LENS not set");
  await win.webContents.executeJavaScript(`window.__selectLens(${JSON.stringify(selectLens)})`);

  const afterPixel = (await win.webContents.executeJavaScript(
    `window.__readCenterPixel()`,
  )) as [number, number, number, number];

  const result = { visible: win.isVisible(), beforePixel, afterPixel };
  process.stdout.write(`${JSON.stringify(result)}\n`);
  app.quit();
});
