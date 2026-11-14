import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { setUnits } from "./units";

import type { PagePayload } from "../shared/api.gen";
import { ControlsPanel } from "./ControlsPanel";
import { visiblePanels } from "./panelRegistry";

let root: Root;
let container: HTMLDivElement;
afterEach(() => { act(() => root.unmount()); container.remove(); setUnits({ length: "mm", angle: "deg" }); });

const motor = { id: "mdl.m", label: "Motor M", facts: [{ label: "Torque constant", value: 0.1, unit: "N*m/A" }], torque_nm: [0, 3, 6], speed_rad_s: [300, 150, 0], efficiency_pct: [0, 80, 0], advice: ["Kv and Kt disagree by 2.0x; expected Kt 0.05 N*m/A. Advice: not yet sourced."] };
const joint = { joint: "shoulder", label: "Shoulder", facts: [{ label: "Overshoot", value: 12.3456, unit: "%" }, { label: "Phase margin", value: 41, unit: "deg" }], t_s: [0, 0.5, 1], position: [0, 1.12, 1], advice: [] };
const pageOf = (o: object) => ({ controls_motors: [], controls_joints: [], ...o }) as unknown as PagePayload;
const mount = (page: PagePayload) => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(<ControlsPanel page={page} />));
};
const labels = (title: string) => Array.from(container.querySelector(`[role="img"][aria-label="${title}"]`)!.querySelectorAll(".chart-label")).map((l) => l.textContent);
const table = (title: string) => Array.from(container.querySelectorAll("table")).find((t) => t.getAttribute("aria-label") === `${title}, data`)!;
const header = (title: string) => Array.from(table(title).querySelectorAll("th")).map((h) => h.textContent);
const column = (title: string, col: number) => Array.from(table(title).querySelectorAll("tbody tr")).map((r) => r.children[col]!.textContent);

describe("ControlsPanel", () => {
  it("draws efficiency in percent and speed in the chosen angle unit (radians here), each against torque in N*m", () => {
    setUnits({ angle: "rad" });
    mount(pageOf({ controls_motors: [motor] }));
    expect(header("Motor M: efficiency against torque")).toEqual(["Torque (N*m)", "Efficiency (%)"]);
    expect(column("Motor M: efficiency against torque", 1)).toEqual(["0", "80", "0"]);
    expect(column("Motor M: efficiency against torque", 0)).toEqual(["0", "3", "6"]);
    expect(labels("Motor M: efficiency against torque")).toEqual(["Efficiency", "Torque (N*m)"]);
    expect(labels("Motor M: speed against torque")).toEqual(["Speed", "Torque (N*m)"]);
    expect(header("Motor M: speed against torque")).toEqual(["Torque (N*m)", "Speed (rad/s)"]);
    expect(column("Motor M: speed against torque", 1)).toEqual(["300", "150", "0"]);
    expect(column("Motor M: speed against torque", 0)).toEqual(["0", "3", "6"]);
  });

  it("shows the card numbers with units and says unsourced advice is not sourced", () => {
    mount(pageOf({ controls_motors: [motor] }));
    expect(container.textContent).toContain("Torque constant0.1 N*m/A");
    expect(container.textContent).toContain("Advice: not yet sourced.");
    expect(container.textContent).not.toContain("—");
  });

  it("plots the joint step as position against seconds with a target plus and minus 2 percent", () => {
    mount(pageOf({ controls_joints: [joint] }));
    expect(header("Shoulder: step response")).toEqual(["Time (s)", "Position", "Target plus 2%", "Target minus 2%"]);
    expect(column("Shoulder: step response", 1)).toEqual(["0", "1.12", "1"]);
    expect(labels("Shoulder: step response")).toEqual(["Position (1 is the target)", "Time (s)"]);
    expect(container.textContent).toContain("Overshoot12.3 %");
    expect(container.textContent).toContain("Phase margin41 deg");
  });

  it("is hidden until the robot has controls data, and a payload without the fields is fine", () => {
    expect(visiblePanels(pageOf({})).some((p) => p.id === "controls")).toBe(false);
    expect(visiblePanels({} as PagePayload).some((p) => p.id === "controls")).toBe(false);
    expect(visiblePanels(pageOf({ controls_joints: [joint] })).some((p) => p.id === "controls")).toBe(true);
    expect(visiblePanels(pageOf({ controls_motors: [{ ...motor, torque_nm: [] }] })).some((p) => p.id === "controls")).toBe(false);
  });
});
