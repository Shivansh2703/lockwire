import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { BrowserWindowConstructorOptions } from "electron";

import { PRODUCT_NAME } from "../shared/branding";

// Pure data, no electron import at runtime (the only reference above is a `type` import, erased
// by `verbatimModuleSyntax`) -- so the flag-absent direction is provable by a plain unit test on
// this object, never by a launch (review lead, TODO.md e2-i2: "testing 'it would be visible' by
// making it visible is testing a fire alarm by starting a fire").
export type Bounds = { x: number; y: number; width: number; height: number };

const overlap = (b: Bounds, a: Bounds): number =>
  Math.max(0, Math.min(b.x + b.width, a.x + a.width) - Math.max(b.x, a.x)) *
  Math.max(0, Math.min(b.y + b.height, a.y + a.height) - Math.max(b.y, a.y));

/** Saved bounds moved (and shrunk if needed) onto the display they overlap most, else the first. */
export function fitBounds(saved: Bounds, areas: Bounds[]): Bounds {
  const area = areas.reduce((best, a) => (overlap(saved, a) > overlap(saved, best) ? a : best), areas[0]!);
  const width = Math.min(saved.width, area.width);
  const height = Math.min(saved.height, area.height);
  const x = Math.min(Math.max(saved.x, area.x), area.x + area.width - width);
  const y = Math.min(Math.max(saved.y, area.y), area.y + area.height - height);
  return { x, y, width, height };
}

export function saveBounds(file: string, b: Bounds): void {
  try {
    writeFileSync(file, JSON.stringify(b));
  } catch {
    // no persistence; the next start opens at the default size
  }
}

/** The last saved bounds, fitted onto a display; undefined when none or unreadable. */
export function loadBounds(file: string, areas: Bounds[]): Bounds | undefined {
  try {
    const b = JSON.parse(readFileSync(file, "utf8")) as Bounds;
    const ok = [b.x, b.y, b.width, b.height].every(Number.isFinite) && b.width > 0 && b.height > 0;
    return ok && areas.length ? fitBounds(b, areas) : undefined;
  } catch {
    return undefined;
  }
}

export function windowOptions(hidden: boolean, bounds?: Bounds): BrowserWindowConstructorOptions {
  return {
    width: 1100,
    height: 750,
    ...bounds,
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
