import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PagePayload } from "../shared/api.gen";
import { PowerPanel } from "./PowerPanel";
vi.mock("./proRegistry", () => ({ pro: null })); // the free build: no pro/ folder
import { visiblePanels } from "./panelRegistry";

let root: Root;
let container: HTMLDivElement;
afterEach(() => { act(() => root.unmount()); container.remove(); });

const stage = (o: object) => ({ rail: "rail.a", label: "Rail A", depth: 0, fed_by: "battery", volts_in: 14.8, volts_out: 5, amps_out: 6.42, efficiency: 0.8, watts_out: 32.1, watts_in: 40.125, watts_lost: 8.025, capacity_w: 15, unknown_loads: 0, loads: [{ id: "Servo1", watts: 7 }, { id: "Camera", watts: null }], ...o });
const pageOf = (...stages: object[]) => ({ power_stages: stages }) as unknown as PagePayload;
const mount = (page: PagePayload, onDeclare = vi.fn().mockResolvedValue(null)) => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(<PowerPanel page={page} onDeclare={onDeclare} />));
  return onDeclare;
};
const click = async (el: Element) => act(async () => { el.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
const typeInto = (label: string, value: string) => act(() => {
  const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
});
const save = () => click(Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Save efficiency")!);

describe("PowerPanel", () => {
  it("shows each stage's volts, current, efficiency and the watts it loses", () => {
    mount(pageOf(stage({})));
    const text = container.textContent!;
    expect(text).toContain("14.8 V in, 5 V out");
    expect(text).toContain("6.42 A out");
    expect(text).toContain("Efficiency 80 %");
    expect(text).toContain("8.03 W lost as heat");
    expect(text).toContain("40.13 W in, 32.1 W out");
  });

  it("hovering a stage lists the parts on it, and says when a draw is missing", () => {
    mount(pageOf(stage({})));
    expect(container.querySelector(".power-parts")!.textContent).not.toContain("Servo1");
    act(() => { container.querySelector(".power-stage")!.dispatchEvent(new MouseEvent("mouseover", { bubbles: true })); });
    const parts = container.querySelector(".power-parts")!.textContent!;
    expect(parts).toContain("Servo1: 7 W");
    expect(parts).toContain("Camera: draw not declared");
  });

  it("saves a typed percent as a 0 to 1 efficiency through declare, and refuses nonsense", async () => {
    const onDeclare = mount(pageOf(stage({})));
    typeInto("efficiency for Rail A", "150");
    await save();
    expect(onDeclare).not.toHaveBeenCalled();
    expect(container.textContent).toContain("Type a percent from 1 to 100.");
    typeInto("efficiency for Rail A", "85");
    await save();
    expect(onDeclare).toHaveBeenCalledWith("topology/rails/rail.a/efficiency", "0.85", "typed in the Power panel");
  });

  it("offers no efficiency entry on a stage the roll-up applies no loss to", () => {
    mount(pageOf(stage({ fed_by: null, efficiency: 1, watts_lost: 0 })));
    expect(container.querySelector('[aria-label="efficiency for Rail A"]')).toBeNull();
    expect(container.textContent).toContain("no loss is counted here");
  });

  it("is hidden until the robot has rails", () => {
    const power = (p: PagePayload) => visiblePanels(p).some((x) => x.id === "power");
    expect(power(pageOf())).toBe(false);
    expect(power(pageOf(stage({})))).toBe(true);
  });

  it("draws no battery chart in the free build, even if the payload carries one", () => {
    const chart = { soc: [{ s: 0, value: 100 }, { s: 60, value: 90 }], volts: [{ s: 0, value: 25 }, { s: 60, value: 24 }], cutoff_v: 20, reserve_pct: 10, cutoff_s: null, cutoff_reason: null };
    mount({ power_stages: [stage({})], battery_chart: chart } as unknown as PagePayload);
    expect(container.querySelector(".power-battery")).toBeNull();
    expect(container.querySelector('[data-testid="chart"]')).toBeNull();
  });
  it("shows light, typical and worst-case watts per rail, each from its own field", () => {
    const page = { power_stages: [stage({})], power_columns: [{ rail: "rail.a", label: "Rail A", light_w: 1.5, typical_w: 2.5, max_w: 3.5, typical_from_plan: false }] } as unknown as PagePayload;
    mount(page);
    const cells = Array.from(container.querySelectorAll(".power-columns tbody td")).map((c) => c.textContent);
    expect(cells).toEqual(["Rail A", "1.5 W", "2.5 W", "3.5 W"]);
  });

  it("lists parts against ratings, calls over-80 percent unsourced advice, and the pack's plan need", () => {
    const row = { part: "Reg", quantity: "output current", value: 2.7, rating: 3, unit: "A", pct: 90, source: "catalog x", over_advice: true };
    const pack = { voltage_v: 14.8, capacity_wh: 74, reserve_pct: 20, usable_wh: 59.2, plan_need_wh: 70 };
    mount({ power_stages: [stage({})], power_stress: [row], power_pack: pack } as unknown as PagePayload);
    expect(container.querySelector(".power-stress")!.textContent).toContain("2.7 of 3 A (90 % of rating)");
    expect(container.querySelector(".power-stress")!.textContent).toContain("advice: not yet sourced");
    expect(container.querySelector(".power-pack")!.textContent).toContain("The plan needs 70 Wh, more than the pack gives");
  });
  it("says Typical follows the plan only when it does, and hides the columns when there are none", () => {
    const col = (f: boolean) => ({ rail: "rail.a", label: "Rail A", light_w: 1, typical_w: 2, max_w: 3, typical_from_plan: f });
    mount({ power_stages: [stage({})], power_columns: [col(true)] } as unknown as PagePayload);
    expect(container.querySelector(".power-columns")!.textContent).toContain("Typical follows your plan.");
    act(() => root.unmount()); container.remove();
    mount({ power_stages: [stage({})], power_columns: [col(false)] } as unknown as PagePayload);
    expect(container.querySelector(".power-columns")!.textContent).not.toContain("follows your plan");
    act(() => root.unmount()); container.remove();
    mount({ power_stages: [stage({})], power_columns: [] } as unknown as PagePayload);
    expect(container.querySelector(".power-columns")).toBeNull();
  });
});
