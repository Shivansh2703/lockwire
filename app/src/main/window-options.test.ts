import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { fitBounds, harnessWindowOptions, loadBounds, saveBounds, windowOptions } from "./window-options";

// The visible-without-the-flag direction is proved HERE, on the options object only -- never by
// a launch. The hidden-under-the-flag direction is also asserted here for
// the option shape, and separately proved by a real launch in hidden-launch.test.ts.
describe("windowOptions: the flag's two directions", () => {
  it("test_flag_absent_show_is_unset_or_true", () => {
    const opts = windowOptions(false);
    expect(opts.show === undefined || opts.show === true).toBe(true);
  });

  it("test_flag_present_show_is_false", () => {
    const opts = windowOptions(true);
    expect(opts.show).toBe(false);
  });
});

// e2-hh3: same shape as windowOptions above, for the e2e check harnesses
// (hidpi-line-check.ts, e2e-pixel-check.ts) -- this replaces e2-hh's unrunnable bite, which
// could only be discharged by forcing `show` off and launching electron for real, which IS the
// violation the flag exists to prevent. Asserted here on the pure options object instead,
// both directions, no electron import, no window.
describe("harnessWindowOptions: the flag's two directions", () => {
  it("test_flag_absent_show_is_unset_or_true", () => {
    const opts = harnessWindowOptions(false);
    expect(opts.show === undefined || opts.show === true).toBe(true);
  });

  it("test_flag_present_show_is_false", () => {
    const opts = harnessWindowOptions(true);
    expect(opts.show).toBe(false);
  });
});

describe("windowOptions: hidden Mac title bar", () => {
  it("test_darwin_hides_the_title_bar_and_keeps_security_flags", () => {
    const real = process.platform;
    Object.defineProperty(process, "platform", { value: "darwin" });
    try {
      const opts = windowOptions(false);
      expect(opts.titleBarStyle).toBe("hiddenInset");
      expect(opts.trafficLightPosition).toBeDefined();
      expect(opts.webPreferences).toMatchObject({ nodeIntegration: false, contextIsolation: true, sandbox: true });
      Object.defineProperty(process, "platform", { value: "linux" });
      expect(windowOptions(false).titleBarStyle).toBeUndefined();
    } finally {
      Object.defineProperty(process, "platform", { value: real });
    }
  });
});

describe("remembered window bounds", () => {
  const screen1 = { x: 0, y: 0, width: 1920, height: 1080 };
  it("saved bounds are restored on the next start, and outside every display they are moved onto one", () => {
    const dir = mkdtempSync(join(tmpdir(), "lw-win-"));
    const file = join(dir, "window.json");
    saveBounds(file, { x: 100, y: 50, width: 900, height: 600 });
    expect(loadBounds(file, [screen1])).toEqual({ x: 100, y: 50, width: 900, height: 600 });
    saveBounds(file, { x: 5000, y: 4000, width: 900, height: 600 });
    expect(loadBounds(file, [screen1])).toEqual({ x: 1020, y: 480, width: 900, height: 600 });
    rmSync(dir, { recursive: true });
  });
  it("a window bigger than the display shrinks to it; no file or no display gives the default", () => {
    expect(fitBounds({ x: -10, y: -10, width: 3000, height: 2000 }, [screen1])).toEqual(screen1);
    expect(loadBounds("/nonexistent/window.json", [screen1])).toBeUndefined();
  });
  it("the saved bounds reach the window options", () => {
    expect(windowOptions(false, { x: 7, y: 8, width: 500, height: 400 })).toMatchObject({ x: 7, y: 8, width: 500, height: 400 });
  });
});
