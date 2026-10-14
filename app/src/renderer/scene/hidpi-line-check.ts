/**
 * Main-process side of the HiDPI resolution-uniform settling check. `devicePixelRatio` is
 * forced to 2 by passing `--force-device-scale-factor=2` on the electron COMMAND LINE (see
 * scene-hidpi.e2e.test.ts's spawnSync args) — `app.commandLine.appendSwitch` from inside this
 * script is too late for this particular Chromium switch (measured: dpr stayed 1). Then this
 * reads back both candidate conventions' line thickness from `hidpi-line-check.html`. Same shape
 * as `e2e-pixel-check.ts` (`show: false`, no screenshot, never on
 * the owner's screen).
 */
import { app, BrowserWindow } from "electron";

import { harnessWindowOptions } from "../../main/window-options";

void app.whenReady().then(async () => {
  app.dock?.hide();
  const win = new BrowserWindow(harnessWindowOptions(true));

  const htmlPath = process.env.LOCKWIRE_E2E_HTML_PATH;
  if (!htmlPath) throw new Error("LOCKWIRE_E2E_HTML_PATH not set");
  await win.loadFile(htmlPath);

  const linewidth = Number(process.env.LOCKWIRE_E2E_LINEWIDTH ?? "8");
  const result = (await win.webContents.executeJavaScript(`window.__runHidpiCheck(${linewidth})`)) as {
    dpr: number;
    cssThickness: number;
    dbThickness: number;
    noFixThickness: number;
    viaBuildSceneThickness: number;
  };

  process.stdout.write(`${JSON.stringify({ visible: win.isVisible(), ...result })}\n`);
  app.quit();
});
