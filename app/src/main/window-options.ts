import { join } from "node:path";

import type { BrowserWindowConstructorOptions } from "electron";

import { PRODUCT_NAME } from "../shared/branding";

// Pure data, no electron import at runtime (the only reference above is a `type` import, erased
// by `verbatimModuleSyntax`) -- so the flag-absent direction is provable by a plain unit test on
// this object, never by a launch (review lead, TODO.md e2-i2: "testing 'it would be visible' by
// making it visible is testing a fire alarm by starting a fire").
export function windowOptions(hidden: boolean): BrowserWindowConstructorOptions {
  return {
    width: 1100,
    height: 750,
    title: PRODUCT_NAME,
    // macOS: no title bar, traffic lights float on the canvas. The renderer's top bar needs
    // `.app-drag` (theme.css) so the window can still be dragged. Linux keeps the native frame.
    ...(process.platform === "darwin"
      ? { titleBarStyle: "hiddenInset" as const, trafficLightPosition: { x: 14, y: 14 } }
      : {}),
    ...(hidden ? { show: false } : {}),
    // Measured in the production spike (spike-prod/main.js): this trio launches.
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      preload: join(__dirname, "../preload/index.js"),
    },
  };
}

// e2-hh3: same pure-options shape as windowOptions() above, for the e2e check harnesses
// (hidpi-line-check.ts, e2e-pixel-check.ts) that load a bare check page rather than the
// production app -- 64x64, no preload, offscreen rendering off. Extracted so the `show` flag's
// two directions are provable on the returned object, never by launching electron and looking
// for a window (the unrunnable bite this box replaces).
export function harnessWindowOptions(hidden: boolean): BrowserWindowConstructorOptions {
  return {
    ...(hidden ? { show: false } : {}),
    width: 64,
    height: 64,
    webPreferences: { offscreen: false },
  };
}
