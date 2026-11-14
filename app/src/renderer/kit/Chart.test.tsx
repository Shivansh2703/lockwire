import { readFileSync } from "node:fs";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";

// @ts-expect-error the repo's contrast script is plain .mjs with no types
import { contrastRatio, THEME_CSS_PATH } from "../../../scripts/contrast.mjs";
import { Chart, type ChartProps } from "./Chart";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root | undefined;
let box: HTMLElement;
function mount(p: Partial<ChartProps>) {
  box = document.createElement("div");
  document.body.appendChild(box);
  root = createRoot(box);
  const props = { series: [], xLabel: "Time", yLabel: "Current", title: "Arm current", ...p };
  act(() => root!.render(<Chart {...props} />));
  return box;
}
function remount(p: Partial<ChartProps>) {
  act(() => root!.unmount());
  box.remove();
  return mount(p);
}
afterEach(() => {
  act(() => root?.unmount());
  box?.remove();
});

const amps = { id: "a", label: "Arm", unit: "A", points: [[0, 1], [1, 2.4], [2, 1.5], [3, 2.6]] as [number, number][] };
const key = (k: string) => act(() => void box.querySelector(".chart-plot")!.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true })));

describe("Chart", () => {
  it("draws one line path per series and is a focusable named image", () => {
    mount({ series: [amps, { ...amps, id: "b", label: "Wrist", kind: "step" }] });
    expect(box.querySelectorAll("path.chart-line")).toHaveLength(2);
    expect(box.querySelectorAll("path.chart-line")[1]!.getAttribute("d")).toContain("V");
    const plot = box.querySelector(".chart-plot")!;
    expect([plot.getAttribute("role"), plot.getAttribute("aria-label"), plot.getAttribute("tabindex")]).toEqual(["img", "Arm current", "0"]);
  });

  it("draws a limit line with its label, and states an over-limit stretch in words", () => {
    mount({ series: [amps], limits: [{ y: 2.2, label: "Motor max", kind: "max" }] });
    expect(box.querySelector(".chart-limit text")!.textContent).toBe("Motor max");
    expect(box.querySelector(".chart-legend")!.textContent).toContain("over max 'Motor max' by 0.4 A");
    expect(box.querySelectorAll("circle.chart-marker")).toHaveLength(2);
    expect(box.querySelector("path.chart-over")).not.toBeNull();
  });

  it("draws the limit label after the dots, with a panel-coloured halo, so nothing hides it", () => {
    mount({ series: [amps], limits: [{ y: 2.2, label: "Motor max", kind: "max" }] });
    const dot = box.querySelector("circle.chart-marker")!, label = box.querySelector(".chart-limit text")!;
    expect(dot.compareDocumentPosition(label) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(readFileSync(THEME_CSS_PATH, "utf8")).toMatch(/\.chart \.chart-limit text\s*\{[^}]*paint-order: stroke;[^}]*stroke: var\(--panel\)/);
  });
  it("says nothing is over when the series stays inside its limit", () => {
    mount({ series: [amps], limits: [{ y: 5, label: "Motor max", kind: "max" }] });
    expect(box.querySelector(".chart-legend")!.textContent).not.toContain("over");
    expect(box.querySelector("circle.chart-marker")).toBeNull();
  });

  it("keeps a limit above the data inside the y axis", () => {
    mount({ series: [amps], limits: [{ y: 50, label: "Far", kind: "max" }] });
    const ticks = [...box.querySelectorAll("text.chart-tick")].map((t) => Number(t.textContent));
    expect(Math.max(...ticks)).toBeGreaterThanOrEqual(50);
  });

  it("gives screen readers a table of the data and notes truncation at 200 rows", () => {
    mount({ series: [amps] });
    expect(box.querySelectorAll("tbody tr")).toHaveLength(4);
    remount({ series: [{ ...amps, points: Array.from({ length: 300 }, (_, i) => [i, i % 7] as [number, number]) }] });
    expect(box.querySelectorAll("tbody tr")).toHaveLength(200);
    expect(box.textContent).toContain("Showing the first 200 of 300 rows.");
  });

  it("Left/Right/Home/End move the readout and the series value follows", () => {
    mount({ series: [amps] });
    const value = () => box.querySelector(".chart-value")!.textContent;
    const readout = () => box.querySelector(".chart-readout")!.textContent;
    expect(value()).toBe("2.6 A");
    key("Home");
    expect([value(), readout()]).toEqual(["1 A", "At 0: Arm 1 A"]);
    key("ArrowRight");
    expect([value(), readout()]).toEqual(["2.4 A", "At 1: Arm 2.4 A"]);
    key("ArrowLeft");
    key("ArrowLeft");
    expect(readout()).toBe("At 0: Arm 1 A");
    key("End");
    expect(readout()).toBe("At 3: Arm 2.6 A");
  });

  it("the readout is a polite live region that says x and every series' value with unit", () => {
    const volts = { id: "v", label: "Servo rail", unit: "V", points: [[0, 5], [1, 5.02], [2, 5], [3, 4.9]] as [number, number][] };
    mount({ series: [amps, volts], xUnit: "s" });
    const r = box.querySelector(".chart-readout")!;
    expect([r.getAttribute("role"), r.getAttribute("aria-live")]).toEqual(["status", "polite"]);
    key("ArrowLeft");
    key("ArrowLeft");
    expect(r.textContent).toBe("At 1 s: Arm 2.4 A, Servo rail 5.02 V");
  });

  it("a min limit says under, marks only points below it and draws the danger stretch", () => {
    const low = { ...amps, points: [[0, 5], [1, 3], [2, 5], [3, 6]] as [number, number][] };
    mount({ series: [low], limits: [{ y: 4, label: "Brown-out", kind: "min" }] });
    const text = box.querySelector(".chart-legend")!.textContent!;
    expect(text).toContain("under min 'Brown-out' by 1 A");
    expect(text).not.toContain("over");
    expect(box.querySelectorAll("circle.chart-marker")).toHaveLength(1);
    expect(box.querySelector("path.chart-over")).not.toBeNull();
  });

  it("keeps limit labels inside the plot and apart from each other", () => {
    const labelYs = () => [...box.querySelectorAll(".chart-limit text")].map((t) => Number(t.getAttribute("y")));
    mount({ series: [amps], height: 220, limits: [{ y: 3, label: "Top", kind: "max" }] });
    expect(labelYs()[0]).toBeGreaterThanOrEqual(34);
    remount({ series: [amps], height: 220, limits: [{ y: 0, label: "Bottom", kind: "min" }] });
    expect(labelYs()[0]).toBeLessThanOrEqual(24 + 158);
    remount({ series: [amps], limits: [{ y: 2, label: "A", kind: "max" }, { y: 2.02, label: "B", kind: "max" }] });
    const [a, b] = labelYs();
    expect(Math.abs(a! - b!)).toBeGreaterThanOrEqual(12);
  });

  it("tick labels stay distinct on an offset range", () => {
    mount({ series: [{ ...amps, points: [[1e6, 1], [1.0001e6, 2]] }] });
    const xs = [...box.querySelectorAll("text.chart-tick")].filter((t) => t.getAttribute("text-anchor") === "middle").map((t) => t.textContent);
    expect(xs.length).toBeGreaterThan(2);
    expect(new Set(xs).size).toBe(xs.length);
  });

  it("lists limits and bands in the hidden data area", () => {
    mount({ series: [amps], xUnit: "s", limits: [{ y: 2.2, label: "Fuse 10 A", kind: "max" }], bands: [{ from: 0, to: 2, label: "Approach" }] });
    const t = box.querySelector("ul.sr-only")!.textContent!;
    expect(t).toContain("Fuse 10 A: max limit 2.2 A");
    expect(t).toContain("Approach: from 0 s to 2 s");
  });

  it("colours only the part of a line, and only the held level of a step, that is beyond the limit", () => {
    mount({ series: [{ ...amps, kind: "step", points: [[0, 1], [1, 5], [2, 1], [3, 1]] }], limits: [{ y: 3, label: "Max", kind: "max" }] });
    const over = box.querySelector("path.chart-over")!.getAttribute("d")!;
    const m = over.match(/^M([\d.]+) ([\d.]+)H([\d.]+)$/)!;
    expect(box.querySelector("path.chart-line")!.getAttribute("d")).toContain(`V${m[2]}H${m[3]}`);
  });

  it("notes mixed units once, unless allowMixedUnits", () => {
    const volts = { ...amps, id: "v", label: "Rail", unit: "V" };
    const note = "These series use different units; compare shapes, not heights.";
    mount({ series: [amps, volts] });
    expect(box.textContent).toContain(note);
    remount({ series: [amps, volts], allowMixedUnits: true });
    expect(box.textContent).not.toContain(note);
    remount({ series: [amps, { ...amps, id: "w" }] });
    expect(box.textContent).not.toContain(note);
  });

  it("arrow, Home and End keys are claimed; Tab and Up/Down are left to the page", () => {
    mount({ series: [amps] });
    const plot = box.querySelector(".chart-plot")!;
    const prevented = (k: string) => {
      const e = new KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true });
      act(() => void plot.dispatchEvent(e));
      return e.defaultPrevented;
    };
    expect(["ArrowLeft", "ArrowRight", "Home", "End"].map(prevented)).toEqual([true, true, true, true]);
    expect(["Tab", "ArrowUp", "ArrowDown"].map(prevented)).toEqual([false, false, false]);
  });

  it("clips a band that runs past the plot on either side", () => {
    mount({ series: [amps], bands: [{ from: -5, to: 1, label: "Early" }, { from: 2, to: 99, label: "Late" }] });
    const [early, late] = [...box.querySelectorAll(".chart-band rect")].map((r) => [Number(r.getAttribute("x")), Number(r.getAttribute("width"))] as const);
    expect(early![0]).toBe(46);
    expect(late![0] + late![1]).toBeLessThanOrEqual(46 + 462);
    expect(late![1]).toBeGreaterThan(0);
  });

  it("renders a sentence, not axes, for no data and for a single point", () => {
    mount({ series: [] });
    expect(box.textContent).toBe("Nothing to plot yet.");
    expect(box.querySelector("svg")).toBeNull();
    remount({ series: [{ ...amps, points: [[1, 1]] }] });
    expect(box.textContent).toContain("Only one point");
  });

  it("never writes NaN into a path for degenerate input", () => {
    mount({
      series: [
        { ...amps, points: [[0, 5], [1, 5], [2, 5]] },
        { id: "n", label: "Noisy", points: [[3, 1], [NaN, 2], [0, Infinity], [1, 4]] },
      ],
      limits: [{ y: 5, label: "Equal", kind: "min" }],
      bands: [{ from: 1, to: 1, label: "empty" }, { from: -9, to: 99, label: "wide" }],
    });
    const ds = [...box.querySelectorAll("path")].map((p) => p.getAttribute("d")!);
    expect(ds.length).toBeGreaterThan(1);
    expect(ds.join("")).not.toMatch(/NaN|Infinity/);
    expect(box.innerHTML).not.toContain("NaN");
    expect(box.textContent).toContain("2 values skipped");
  });

  it("limits a 10,000-point series to a drawable number of points", () => {
    mount({ series: [{ ...amps, points: Array.from({ length: 10000 }, (_, i) => [i, i === 500 ? 40 : 1] as [number, number]) }] });
    expect(box.querySelector("path.chart-line")!.getAttribute("d")!.split(/(?=[ML])/).length).toBeLessThanOrEqual(600);
  });
});

describe("chart colours", () => {
  it("the plot lets the page scroll vertically on touch", () => {
    expect(readFileSync(THEME_CSS_PATH, "utf8")).toMatch(/\.chart-plot\s*\{[^}]*touch-action: pan-y/);
  });

  it("every series colour is at least 3:1 on the panel, in both themes", () => {
    const css = readFileSync(THEME_CSS_PATH, "utf8");
    const blocks = [css.match(/:root\[data-theme="light"\]\s*\{([^}]*)\}/)?.[1] ?? "", css.match(/:root\[data-theme="dark"\]\s*\{([^}]*)\}/)?.[1] ?? ""];
    for (const b of blocks) {
      const panel = b.match(/--panel:\s*(#\w+)/)![1];
      for (let n = 1; n <= 6; n++) {
        const c = b.match(new RegExp(`--chart-${n}:\\s*(#\\w+)`))![1];
        expect(contrastRatio(c, panel)).toBeGreaterThanOrEqual(3);
      }
    }
  });
});
