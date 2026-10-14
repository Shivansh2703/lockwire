import { app, BrowserWindow } from "electron";

import { windowOptions } from "./window-options";

// Not a test file itself: hidden-launch.test.ts bundles this with esbuild and runs it under the
// REAL electron binary, as its own process -- the only way to prove a hidden window is actually
// invisible, since Playwright's `_electron.launch()` has no headless option.
// It builds the window with the SAME windowOptions() the production app uses, so a regression
// there (e.g. removing `show: false`) fails this exact check.
void app.whenReady().then(() => {
  app.dock?.hide();
  const win = new BrowserWindow(windowOptions(true));
  const result = {
    visible: win.isVisible(),
    dockVisible: process.platform === "darwin" ? (app.dock?.isVisible() ?? false) : false,
  };
  process.stdout.write(`${JSON.stringify(result)}\n`);
  app.quit();
});
