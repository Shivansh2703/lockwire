import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { PAGE_WITH_SCENE, PLAN, goMode, mockMatchMedia, picked } from "./App.testkit";
import type { PagePayload } from "../shared/api.gen";
import type { AssistantShow } from "../shared/ipc-contract";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import ARM_POWER from "./armPower.page.fixture.json";
import { getDesignView, resetDesignView } from "./scene/designView";
import { applyIsolate } from "./scene/robotScene";
import { getSelectedPartId, setSelectedPartId } from "./scene/selectionStore";
import { getViewState, setOnlyParts } from "./scene/viewState";

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
let show: (m: AssistantShow) => void; // the listener App gave the preload: what a show message from main calls
beforeEach(() => { mockMatchMedia(); setSelectedPartId(null); vi.mocked(applyIsolate).mockClear(); container = document.createElement("div"); document.body.appendChild(container); });
afterEach(() => { act(() => root.unmount()); container.remove(); Reflect.deleteProperty(window, "lockwire"); });

async function open(page: unknown = PAGE) {
  (window as unknown as { lockwire: object }).lockwire = {
    startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: true, path: "/tmp/r.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD })),
    getContractPage: vi.fn().mockResolvedValue(page), getDeclareForm: vi.fn().mockResolvedValue(FORM), getHistory: vi.fn().mockResolvedValue([]),
    choosePlan: vi.fn().mockResolvedValue("/tmp/plan.yaml"), checkPlan: vi.fn().mockResolvedValue(PLAN),
    onAssistantShow: (cb: typeof show) => { show = cb; return () => undefined; },
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

  it("show tools: a select-part and a show-only message from the Assistant change the selection and the only-list at once, in 3D and in the graph, as a click does", async () => {
    setOnlyParts([]);
    await open();
    const say = (m: object, contract = "/tmp/r.yaml") => act(() => show({ contract, ...m } as AssistantShow));
    say({ action: "select-part", part: "dev.imu" });
    expect(getSelectedPartId()).toBe("dev.imu");
    say({ action: "select-part", part: "dev.cam" }); // a part with a finding: its card opens and the selection clears, exactly as a click on it does
    const told = getSelectedPartId();
    click(container.querySelector('[data-pick="dev.cam"]'));
    expect([told, getSelectedPartId()]).toEqual([null, null]);
    say({ action: "show-only", parts: ["dev.imu"] }, "/tmp/another.yaml"); // another robot's turn moves nothing here
    expect(getViewState().only).toEqual([]);
    say({ action: "show-only", parts: ["dev.cam", "dev.unplaced"], finding: 0 });
    expect(getViewState().only).toEqual(["dev.cam"]); // only what this window draws: the view is never left empty
    expect(lastIsolate()).toEqual(["dev.cam"]);
    say({ action: "open-graph" });
    expect(nodes()).toEqual(["battery", "device:dev.cam", "rail:rail.5v"]); // the graph keeps the finding's nodes
    say({ action: "show-all" });
    expect(getViewState().only).toEqual([]);
    expect(nodes()).toHaveLength(4);
    say({ action: "show-connections", lens: "power" });
    expect([getDesignView().lens, container.querySelector('[data-testid="graph-view"]'), container.querySelector('[role="tab"][aria-selected="true"]')?.textContent]).toEqual(["power", null, "Design"]);
    expect(lastIsolate()).toBeNull();
    resetDesignView();
  });
  it("a finding always shows something: on the real arm-power page, show-only on the brown-out narrows the graph from 20 boxes to its 8 and opens it", async () => {
    setOnlyParts([]);
    await open(ARM_POWER);
    const say = (m: object) => act(() => show({ contract: "/tmp/r.yaml", ...m } as AssistantShow));
    say({ action: "open-graph" });
    expect(nodes()).toHaveLength(20);
    say({ action: "show-only", parts: [], finding: 0 });
    expect(nodes()).toHaveLength(8);
    expect(nodes()).toEqual([...ARM_POWER.cards[0]!.involves].sort());
    say({ action: "show-all" });
    expect(nodes()).toHaveLength(20);
    click(button("Graph")); // closed again
    expect(container.querySelector('[data-testid="graph-view"]')).toBeNull();
    say({ action: "show-only", parts: [], finding: 0 }); // nothing drawn narrows, graph closed: it opens by itself
    expect(nodes()).toHaveLength(8);
  });

  it("the 3D view is never narrowed to parts with no mesh: the assistant's show-only and a person's own tap leave 3D on null while the graph narrows; a drawn finding still narrows 3D", async () => {
    setOnlyParts([]);
    await open(ARM_POWER);
    const say = (m: object) => act(() => show({ contract: "/tmp/r.yaml", ...m } as AssistantShow));
    say({ action: "show-only", parts: [], finding: 0 });
    expect(nodes()).toHaveLength(8); // the assistant opened the graph on the brown-out's 8 nodes
    click(button("Graph")); // closed
    expect(lastIsolate() ?? null).toBeNull();
    say({ action: "show-all" });
    click(button("Only what this check uses")); // a person's own tap on the same finding
    expect(lastIsolate() ?? null).toBeNull();
    click(button("Graph"));
    expect(nodes()).toHaveLength(8);
  });

  it("a finding whose parts are drawn still narrows the 3D view to them, by the assistant or a tap", async () => {
    setOnlyParts([]);
    await open();
    act(() => show({ contract: "/tmp/r.yaml", action: "show-only", parts: ["dev.cam"], finding: 0 } as AssistantShow));
    expect(lastIsolate()).toEqual(["dev.cam"]);
  });

  it("show-all brings back the 3D list and the graph; a message of the wrong shape changes nothing and throws nothing", async () => {
    setOnlyParts([]);
    await open();
    const say = (m: unknown) => act(() => show(m as AssistantShow));
    const at = (m: object) => ({ contract: "/tmp/r.yaml", ...m });
    say(at({ action: "show-only", parts: ["dev.cam"], finding: 0 }));
    click(button("Graph"));
    expect([getViewState().only, nodes()]).toEqual([["dev.cam"], ["battery", "device:dev.cam", "rail:rail.5v"]]);
    say(at({ action: "show-all" }));
    expect([getViewState().only, nodes().length]).toEqual([[], 4]);
    const before = container.innerHTML;
    const state = () => [getViewState().only, getSelectedPartId(), getDesignView().lens, container.innerHTML];
    const was = state();
    for (const bad of [at({ action: "launch" }), at({ action: "show-only" }), at({ action: "show-only", parts: "dev.cam" }), at({ action: "show-only", parts: [], finding: 99 }),
      at({ action: "show-only", parts: [], finding: -1 }), at({ action: "show-only", parts: [], finding: 1.5 }), at({ action: "show-only", parts: [], finding: "0" }),
      at({ action: "show-connections", lens: "health" }), at({ action: "show-connections", lens: "<b>x</b>" }), at({ action: "select-part", part: "nothing.here" }),
      at({ action: "select-part", part: 5 }), at({ action: "show-only", parts: [7] }), at({ action: "show-only", parts: ["nope.a", "nope.b"] }), at({ action: "show-only", parts: ["nope.a"], finding: 0 }),
      Object.create(at({ action: "open-graph" })), Object.create({ contract: "/tmp/r.yaml" }, { action: { value: "open-graph", enumerable: true } }), null, undefined]) {
      say(bad);
      expect(state()).toEqual(was);
    }
    expect(container.innerHTML).toBe(before);
  });
});
