import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { PAGE_WITH_SCENE, PLAN, goMode, mockMatchMedia, picked } from "./App.testkit";
import type { PagePayload } from "../shared/api.gen";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import { applyIsolate } from "./scene/robotScene";
import { getSelectedPartId, setSelectedPartId } from "./scene/selectionStore";

// GRAPH-1 through the real App: the switch, the first result, picking a part, isolating in 3D.
vi.mock("./scene/Canvas", async () => {
  const store = await vi.importActual<typeof import("./scene/selectionStore")>("./scene/selectionStore");
  return {
    Canvas: ({ children, parts = [], onPick }: { children?: React.ReactNode; parts?: { id: string }[]; onPick?: (id: string) => void }) => (
      <div data-testid="scene-canvas">
        {parts.map((p) => <button key={p.id} type="button" data-pick={p.id} onClick={() => { store.setSelectedPartId(p.id); onPick?.(p.id); }} />)}
        {children}
      </div>
    ),
    useScene: () => ({ meshes: [] }),
  };
});
vi.mock("./scene/robotScene", async (orig) => ({ ...(await orig<typeof import("./scene/robotScene")>()), applyHighlight: vi.fn(), applyDim: vi.fn(), applyIsolate: vi.fn(), focusCamera: vi.fn() }));

const part = (id: string, label: string) => ({ ...(PAGE_WITH_SCENE.scene as { parts: object[] }).parts[0], id, node: id, name: label, group: { id, label, kind: "device" } });
const CARD = {
  ...X500_CHECK_PAYLOAD.cards[0]!,
  severity: "violation" as const, shape: "power/rail", title: "Rail 5 V overloaded", quantity: "budgets/bud.power.5v", owner: "power-integration", whole: false,
  targets: [{ kind: "rail" as const, id: "rail.5v", label: "Rail 5 V", parts: ["rail.5v", "dev.cam"] }],
  involves: ["rail:rail.5v", "device:dev.cam", "battery"],
};
const PAGE = {
  ...PAGE_WITH_SCENE,
  cards: [CARD],
  scene: { ...(PAGE_WITH_SCENE.scene as object), parts: [part("dev.cam", "Camera"), part("dev.imu", "Imu"), part("rail.5v", "Rail 5 V")] },
  robot_graph: {
    nodes: [
      { id: "device:dev.cam", kind: "device", label: "Camera" }, { id: "device:dev.imu", kind: "device", label: "Imu" },
      { id: "rail:rail.5v", kind: "rail", label: "Rail 5 V" }, { id: "battery", kind: "battery", label: "battery" },
    ],
    edges: [{ from: "rail:rail.5v", to: "device:dev.cam", kind: "power" }, { from: "battery", to: "rail:rail.5v", kind: "power" }],
  },
} as unknown as PagePayload;
const FORM = { revision: "revision", rows: [], witness: { prefill: "Maya" } };

let container: HTMLDivElement;
let root: Root;
beforeEach(() => { mockMatchMedia(); setSelectedPartId(null); vi.mocked(applyIsolate).mockClear(); container = document.createElement("div"); document.body.appendChild(container); });
afterEach(() => { act(() => root.unmount()); container.remove(); Reflect.deleteProperty(window, "lockwire"); });

async function open() {
  (window as unknown as { lockwire: object }).lockwire = {
    startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: true, path: "/tmp/r.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD })),
    getContractPage: vi.fn().mockResolvedValue(PAGE), getDeclareForm: vi.fn().mockResolvedValue(FORM), getHistory: vi.fn().mockResolvedValue([]),
    choosePlan: vi.fn().mockResolvedValue("/tmp/plan.yaml"), checkPlan: vi.fn().mockResolvedValue(PLAN),
  };
  root = createRoot(container);
  act(() => root.render(<App />));
  await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Open robot")!.click());
  await act(async () => {});
}
const click = (el: Element | null | undefined) => act(() => { el!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
const button = (text: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === text);
const nodes = () => [...container.querySelectorAll("[data-node]")].map((n) => n.getAttribute("data-node")!).sort();
const lastIsolate = () => vi.mocked(applyIsolate).mock.lastCall?.[1];

describe("graph view in the app", () => {
  it("the first check result is open with the graph still off: the switch adds no tap", async () => {
    await open();
    expect(container.querySelector(".finding-card")?.textContent).toContain("Rail 5 V overloaded");
    expect(button("Graph")?.getAttribute("aria-pressed")).toBe("false");
    expect(container.querySelector('[data-testid="graph-view"]')).toBeNull();
  });

  it("the 3D gesture hint is gone while the graph shows and back with the 3D view", async () => {
    await open();
    expect(container.querySelector(".shell-hint")).not.toBeNull();
    click(button("Graph"));
    expect(container.querySelector(".shell-hint")).toBeNull();
    click(button("Graph"));
    expect(container.querySelector(".shell-hint")).not.toBeNull();
  });

  it("the Graph switch sits in the same place on Design, Check and Plan", async () => {
    await open();
    const where = () => { const b = button("Graph")!; return `${b.closest(".kit-toolpill, [aria-label]")?.getAttribute("aria-label")}:${[...b.parentElement!.children].indexOf(b)}`; };
    const check = where();
    for (const m of ["Design", "Plan"]) { goMode(container, m); expect(where()).toBe(check); }
  });

  it("one tap opens the graph with the open check already lit; a node picks its part like a 3D pick", async () => {
    await open();
    click(button("Graph"));
    expect(container.querySelectorAll(".gv-node.is-lit")).toHaveLength(3);
    goMode(container, "Design");
    click(container.querySelector('[data-node="device:dev.imu"]'));
    const fromGraph = getSelectedPartId();
    setSelectedPartId(null);
    click(button("Graph"));
    click(container.querySelector('[data-pick="dev.imu"]'));
    expect(fromGraph).toBe("dev.imu");
    expect(getSelectedPartId()).toBe(fromGraph);
  });

  it("one tap on 'Only what this check uses' hides the other parts in the graph and in 3D, another brings them back", async () => {
    await open();
    expect(lastIsolate() ?? null).toBeNull();
    click(button("Only what this check uses"));
    expect(lastIsolate()).toEqual(["dev.cam"]);
    click(button("Graph"));
    expect(nodes()).toEqual(["battery", "device:dev.cam", "rail:rail.5v"]);
    click(button("Graph"));
    click(button("Only what this check uses"));
    expect(lastIsolate()).toBeNull();
    click(button("Graph"));
    expect(nodes()).toHaveLength(4);
  });

  it("a picked rail's 'Only these' goes through the shared 3D isolate and survives leaving the graph", async () => {
    await open();
    click(button("Graph"));
    click(container.querySelector('[data-node="rail:rail.5v"]'));
    expect(container.querySelector(".gv-focus")?.textContent).toContain("Feeds: Camera");
    click(button("Only these"));
    expect(nodes()).toEqual(["device:dev.cam", "rail:rail.5v"]);
    click(button("Graph"));
    expect(lastIsolate()).toEqual(["dev.cam"]);
  });
});
