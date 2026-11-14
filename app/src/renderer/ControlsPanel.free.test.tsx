import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PagePayload } from "../shared/api.gen";
import { ControlsPanel } from "./ControlsPanel";

vi.mock("./proRegistry", () => ({ pro: null })); // the free build: no pro/ folder
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let root: Root;
let box: HTMLDivElement;
afterEach(() => { act(() => root.unmount()); box.remove(); });

describe("the free Controls panel", () => {
  it("renders no tuner, even with a lockfile and a joint", () => {
    const joint = { joint: "shoulder", label: "Shoulder", facts: [], t_s: [0, 1], position: [0, 1], advice: [] };
    box = document.createElement("div");
    document.body.appendChild(box);
    root = createRoot(box);
    act(() => root.render(<ControlsPanel page={{ controls_motors: [], controls_joints: [joint] } as unknown as PagePayload} contract="/r/lock.yaml" />));
    expect(box.querySelector("section.controls-joint")).not.toBeNull();
    expect(box.querySelector(".controls-tuner")).toBeNull();
    expect(box.querySelector('input[type="range"]')).toBeNull();
  });
});
