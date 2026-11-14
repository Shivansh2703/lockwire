import { describe, expect, it } from "vitest";

import type { PagePayload } from "../shared/api.gen";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { InterferencePanel, say, saySweep, sweepNote } from "./InterferencePanel";
import { panels } from "./panelRegistry";
import { checkPair } from "./scene/collide";

const I = [1, 0, 0, 0, 1, 0, 0, 0, 1];
const cube = (x: number) => ({ kind: "box" as const, size: [0.1, 0.1, 0.1], position: [x, 0, 0], rotation: I });

describe("Overlaps panel", () => {
  it("is hidden with no primitives and shown with one", () => {
    const entry = panels.find((p) => p.id === "interference")!;
    expect(entry.hasData({ links: [{ name: "a", position: [0, 0, 0], solids: [] }] } as unknown as PagePayload)).toBe(false);
    expect(entry.hasData({ links: [{ name: "a", position: [0, 0, 0], solids: [cube(0)] }] } as unknown as PagePayload)).toBe(true);
  });
  it("says two overlapping boxes in cubic centimetres and a gap in millimetres", () => {
    expect(say({ ...checkPair(cube(0), cube(0.05)), a: "Arm", b: "Hand" })).toBe("Arm and Hand overlap by 500.0 cm³");
    expect(say({ ...checkPair(cube(0), cube(0.1005)), a: "Arm", b: "Hand" })).toBe("Arm and Hand are 0.5 mm apart");
  });
  it("labels the 5 mm tight threshold as a rule of thumb", () => {
    const page = { joints: [], links: [{ name: "a", position: [0, 0, 0], solids: [cube(0)] }] } as unknown as PagePayload;
    expect(renderToStaticMarkup(createElement(InterferencePanel, { page }))).toContain("Tight means under 5 mm (rule of thumb).");
  });
  it("says a hit with its angle and lists the section only when a joint exists", () => {
    expect(saySweep({ joint: "Elbow", unit: "deg", step: 5, hit: { a: "Elbow", b: "Base", at: 120 }, closest: null })).toBe("Elbow hits Base at 120 deg");
    expect(saySweep({ joint: "Shoulder", unit: "deg", step: 5, hit: { a: "Elbow", b: "Base", at: 120 }, closest: null })).toBe("Shoulder joint: Elbow hits Base at 120 deg");
    const page = { joints: [], links: [{ name: "a", position: [0, 0, 0], solids: [cube(0)] }] } as unknown as PagePayload;
    expect(renderToStaticMarkup(createElement(InterferencePanel, { page }))).not.toContain("Through the joint range");
  });
  it("never says none found above a joint-range hit", () => {
    const solid = (position: number[], size: number[]) => ({ kind: "box" as const, size, position, rotation: I });
    const link = (name: string, solids: unknown[]) => ({ name, position: [0, 0, 0], solids });
    const joint = (name: string, type: string, child: string, upper: number | null) => ({ name, type, parent: "base", child, parent_name: "Base", child_name: name, position: [0, 0, 0], axis: [0, 0, 1], lower: upper === null ? null : 0, upper });
    const page = (upper: number) => ({ collision_pairs: [], links: [link("base", []), link("arm", [solid([0.5, 0, 0], [1, 0.02, 0.02])]), link("post", [solid([-0.4, 0.69, 0], [0.02, 0.02, 0.02])])], joints: [joint("elbow", "revolute", "arm", upper), joint("pin", "fixed", "post", null)] }) as unknown as PagePayload;
    const html = (p: PagePayload) => renderToStaticMarkup(createElement(InterferencePanel, { page: p }));
    expect(html(page(Math.PI))).not.toContain("No overlapping or tight parts found.");
    expect(html(page(0.5))).toContain("No overlapping or tight parts found.");
  });
  it("says the step size per joint and that a thin contact between steps can be missed", () => {
    const note = sweepNote([{ joint: "Elbow", unit: "deg", step: 5, hit: null, closest: null }, { joint: "Slide", unit: "mm", step: 27.8, hit: null, closest: null }]);
    expect(note).toContain("Elbow 5.0 deg, Slide 27.8 mm");
    expect(note).toContain("a thin contact between steps can be missed");
  });
  it("shows each joint's computed step size in the panel: a 2.4 rad range is 3.8 deg, a 0.5 m slide 13.9 mm", () => {
    const jt = (type: string, child: string, name: string, upper: number) => ({ name: child, type, parent: "base", child, parent_name: "Base", child_name: name, position: [0, 0, 0], axis: [0, 0, 1], lower: 0, upper });
    const links = [{ name: "base", position: [0, 0, 0], solids: [cube(0)] }, { name: "arm", position: [0, 0, 0], solids: [cube(1)] }, { name: "slide", position: [0, 0, 0], solids: [cube(-1)] }];
    const page = { joints: [jt("revolute", "arm", "Elbow", 2.4), jt("prismatic", "slide", "Slide", 0.5)], links } as unknown as PagePayload;
    expect(renderToStaticMarkup(createElement(InterferencePanel, { page }))).toContain("Step size: Elbow 3.8 deg, Slide 13.9 mm.");
  });
});
