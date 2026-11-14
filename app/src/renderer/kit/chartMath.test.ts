import { describe, expect, it } from "vitest";

import { cleanPoints, decimate, extent, formatTicks, formatValue, nearestIndex, niceTicks, placeLabels, splitAtLimit, type Pt } from "./chartMath";

describe("chartMath", () => {
  it("niceTicks: 1/2/5 steps for plain, negative and tiny ranges", () => {
    expect(niceTicks(0, 10)).toEqual([0, 2, 4, 6, 8, 10]);
    expect(niceTicks(-3, 3)).toEqual([-4, -2, 0, 2, 4]);
    expect(niceTicks(0, 0.0003)).toEqual([0, 0.0001, 0.0002, 0.0003]);
    expect(niceTicks(0.12, 0.93).at(-1)).toBeGreaterThanOrEqual(0.93);
  });

  it("niceTicks: equal ends still give a range", () => {
    const t = niceTicks(5, 5);
    expect(t[0]).toBeLessThan(5);
    expect(t.at(-1)).toBeGreaterThan(5);
  });

  it("extent includes the limits and pads equal values", () => {
    expect(extent([1, 2, 3], [10, -4])).toEqual([-4, 10]);
    expect(extent([7, 7])).toEqual([6.3, 7.7]);
    expect(extent([])).toBeNull();
  });

  it("cleanPoints drops NaN/Infinity, counts them and sorts by x", () => {
    const r = cleanPoints([[2, 1], [1, NaN], [Infinity, 3], [0, 5]]);
    expect(r.points).toEqual([[0, 5], [2, 1]]);
    expect(r.dropped).toBe(2);
  });

  it("decimate keeps a one-sample spike and caps the count", () => {
    const pts: Pt[] = Array.from({ length: 10000 }, (_, i) => [i, i === 4321 ? 99 : Math.sin(i / 50)]);
    pts[7777] = [7777, -50];
    const out = decimate(pts);
    expect(out.length).toBeLessThanOrEqual(600);
    expect(Math.max(...out.map((p) => p[1]))).toBe(99);
    expect(Math.min(...out.map((p) => p[1]))).toBe(-50);
    expect(out[0]).toBe(pts[0]);
    expect(out.at(-1)).toBe(pts[9999]);
    expect(out.every((p, i) => i === 0 || p[0] >= (out[i - 1] as Pt)[0])).toBe(true);
  });

  it("decimate leaves short input alone", () => {
    const pts: Pt[] = [[0, 1], [1, 2]];
    expect(decimate(pts)).toBe(pts);
  });

  it("nearestIndex picks the closest x, ties go left, clamps at the ends", () => {
    const xs = [0, 10, 20, 40];
    expect(nearestIndex(xs, 11)).toBe(1);
    expect(nearestIndex(xs, 31)).toBe(3);
    expect(nearestIndex(xs, 30)).toBe(2);
    expect(nearestIndex(xs, -5)).toBe(0);
    expect(nearestIndex(xs, 99)).toBe(3);
    expect(nearestIndex([], 1)).toBe(-1);
  });

  it("formatValue: three significant digits, unit appended", () => {
    expect(formatValue(1234.5)).toBe("1230");
    expect(formatValue(0.012345, "A")).toBe("0.0123 A");
    expect(formatValue(2, "s")).toBe("2 s");
    expect(formatValue(0.4)).toBe("0.4");
  });

  it("formatTicks: neighbours never print the same, on offset and tiny ranges", () => {
    for (const [lo, hi] of [[1e6, 1.0001e6], [0.0012, 0.0019], [0, 10]] as const) {
      const t = formatTicks(niceTicks(lo, hi));
      expect(new Set(t).size).toBe(t.length);
    }
    expect(formatTicks([0, 0.5, 1])).toEqual(["0.0", "0.5", "1.0"]);
  });

  it("splitAtLimit cuts a crossing segment at the limit, in both directions, and on a polyline that comes back", () => {
    const max = { y: 2, kind: "max" as const };
    expect(splitAtLimit([0, 0], [1, 4], max)).toEqual([[0.5, 2], [1, 4]]);
    expect(splitAtLimit([1, 4], [2, 0], max)).toEqual([[1, 4], [1.5, 2]]);
    expect(splitAtLimit([0, 3], [1, 5], max)).toEqual([[0, 3], [1, 5]]);
    expect(splitAtLimit([0, 0], [1, 1], max)).toBeNull();
    expect(splitAtLimit([0, 4], [1, 0], { y: 2, kind: "min" })).toEqual([[0.5, 2], [1, 0]]);
  });

  it("placeLabels clamps into the plot and moves a crowded label", () => {
    expect(placeLabels([5, 500], 30, 100)).toEqual([30, 100]);
    expect(placeLabels([50, 52], 30, 100)).toEqual([50, 62]);
    expect(placeLabels([100, 100], 30, 100)).toEqual([100, 88]);
  });
});
