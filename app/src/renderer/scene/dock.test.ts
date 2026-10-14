import { describe, expect, it } from "vitest";

import { dockCallouts } from "./dock";

function overlaps(a: { x: number; y: number; width: number; height: number }, b: typeof a): boolean {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}

describe("dockCallouts", () => {
  it("test_the_docking_pass_leaves_no_two_callouts_overlapping", () => {
    const boxes = [
      { id: "a", x: 100, y: 100, width: 120, height: 24 },
      { id: "b", x: 105, y: 105, width: 120, height: 24 }, // deliberately overlapping "a"
      { id: "c", x: 108, y: 108, width: 120, height: 24 }, // overlaps both once nudged
    ];
    const docked = dockCallouts(boxes, { width: 800, height: 600 });
    for (let i = 0; i < docked.length; i++) {
      for (let j = i + 1; j < docked.length; j++) {
        expect(overlaps(docked[i]!, docked[j]!)).toBe(false);
      }
    }
  });

  it("clamps a callout inside the panel instead of letting it hang off the edge", () => {
    const docked = dockCallouts([{ id: "a", x: -50, y: 900, width: 120, height: 24 }], {
      width: 800,
      height: 600,
    });
    expect(docked[0]!.x).toBeGreaterThanOrEqual(0);
    expect(docked[0]!.x + docked[0]!.width).toBeLessThanOrEqual(800);
    expect(docked[0]!.y).toBeGreaterThanOrEqual(0);
  });
});
