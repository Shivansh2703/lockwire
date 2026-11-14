import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Card, RobotGraph } from "../shared/api.gen";
import { GraphView } from "./GraphView";
import { isolateIds, layout } from "./graphLayout";
import { getViewState, setOnlyParts } from "./scene/viewState";

const n = (id: string, label = id.split(":")[1] ?? id) => ({ id, label, kind: id.split(":")[0] as "link" });
const GRAPH = {
  nodes: [n("link:base"), n("joint:j1"), n("link:arm"), n("device:servo", "Servo"), n("rail:r5", "5 V rail"), { id: "battery", kind: "battery", label: "battery" }],
  edges: [
    { from: "link:base", to: "joint:j1", kind: "structure" }, { from: "joint:j1", to: "link:arm", kind: "structure" },
    { from: "device:servo", to: "joint:j1", kind: "drives" }, { from: "rail:r5", to: "device:servo", kind: "power" }, { from: "battery", to: "rail:r5", kind: "power" },
  ],
} as RobotGraph;
const card = (title: string, involves: string[]) => ({ title, involves, observed: { value: 9.456, unit: "W", kind: "derived", by: "x" }, expected: null }) as unknown as Card;
const CARDS = [card("Servo too weak", ["joint:j1", "device:servo", "link:arm"]), card("Rail overloaded", ["rail:r5", "battery", "device:servo"])];

const INV = CARDS.map((c) => c.involves ?? []);
let el: HTMLDivElement, root: Root;
beforeEach(() => { el = document.createElement("div"); document.body.appendChild(el); root = createRoot(el); });
afterEach(() => { act(() => root.unmount()); el.remove(); });
const draw = (p: Partial<Parameters<typeof GraphView>[0]> = {}) => act(() => root.render(
  <GraphView graph={GRAPH} cards={CARDS} selected={0} onSelectCard={() => {}} isolate={false} selectedPart={null} onPick={() => {}} {...p} />));
const tap = (id: string) => act(() => { el.querySelector(`[data-node="${id}"]`)!.dispatchEvent(new MouseEvent("click", { bubbles: true })); });
const ids = (sel: string) => [...el.querySelectorAll(sel)].map((x) => x.getAttribute("data-node")).sort();

describe("graph view", () => {
  it("opens fitted to the checked parts, and Fit shows the whole graph", () => {
    draw();
    const view = () => el.querySelector("svg > g")!.getAttribute("transform");
    expect(view()).not.toBe("translate(40 40) scale(1)");
    const opened = view();
    act(() => { [...el.querySelectorAll("button")].find((b) => b.textContent === "Fit")!.click(); });
    expect(view()).not.toBe(opened);
  });
  it("opens readable on a very wide graph: labels stay near 11px, Fit still shrinks to the whole", () => {
    const ids = Array.from({ length: 40 }, (_, i) => `link:p${i}`);
    const wide = { nodes: ids.map((i) => n(i)), edges: ids.slice(1).map((to, i) => ({ from: ids[i]!, to, kind: "structure" })) } as RobotGraph;
    draw({ graph: wide, cards: [card("One", ["link:p20"])] });
    const k = () => Number(/scale\(([\d.]+)\)/.exec(el.querySelector("svg > g")!.getAttribute("transform")!)![1]);
    expect(k()).toBeGreaterThanOrEqual(0.95);
    act(() => { [...el.querySelectorAll("button")].find((b) => b.textContent === "Fit")!.click(); });
    expect(k()).toBeLessThan(0.5);
  });
  const chain = (m: number) => { const c = Array.from({ length: m }, (_, i) => `link:p${i}`); return { nodes: c.map((i) => n(i)), edges: c.slice(1).map((to, i) => ({ from: c[i]!, to, kind: "structure" })) } as RobotGraph; };
  const at = (id: string) => { const m = /translate\(([-\d.]+) ([-\d.]+)\) scale\(([\d.]+)\)/.exec(el.querySelector("svg > g")!.getAttribute("transform")!)!; const p = /translate\(([-\d.]+) /.exec(el.querySelector(`[data-node="${id}"]`)!.getAttribute("transform") ?? "") ; return Number(m[1]) + (p ? Number(p[1]) : 0) * Number(m[3]); };
  it("opens centred on the checked parts, with a far-away part out of view", () => {
    draw({ graph: chain(40), cards: [card("One", ["link:p20"])] });
    expect(at("link:p20")).toBeGreaterThan(300);
    expect(at("link:p20")).toBeLessThan(600);
    expect(at("link:p0")).toBeLessThan(0);
  });
  it("keeps labels readable when the checked parts and their neighbours are wider than the window", () => {
    draw({ graph: chain(40), cards: [card("One", ["link:p0", "link:p39"])] });
    expect(Number(/scale\(([\d.]+)\)/.exec(el.querySelector("svg > g")!.getAttribute("transform")!)![1])).toBeGreaterThanOrEqual(0.95);
  });
  it("brings a newly chosen check's parts into view, but not after the user has panned", () => {
    const g = chain(40), cs = [card("A", ["link:p0"]), card("B", ["link:p30"])];
    draw({ graph: g, cards: cs, selected: 0 });
    draw({ graph: g, cards: cs, selected: 1 });
    expect(at("link:p30")).toBeGreaterThan(0);
    expect(at("link:p30")).toBeLessThan(900);
    draw({ graph: g, cards: cs, selected: 0 });
    act(() => { (el.querySelector('[aria-label="Zoom in"]') as HTMLElement).click(); });
    const kept = at("link:p0");
    draw({ graph: g, cards: cs, selected: 1 });
    expect(at("link:p0")).toBe(kept);
  });
  it("lights exactly the parts the chosen check involves, and follows the choice", () => {
    draw();
    expect(ids(".gv-node.is-lit")).toEqual([...INV[0]!].sort());
    draw({ selected: 1 });
    expect(ids(".gv-node.is-lit")).toEqual([...INV[1]!].sort());
  });

  it("lights an edge only when both its ends are involved", () => {
    draw();
    const lit = [...el.querySelectorAll(".gv-edge.is-lit")].map((e) => `${e.getAttribute("data-from")}>${e.getAttribute("data-to")}`);
    expect(lit.sort()).toEqual(["device:servo>joint:j1", "joint:j1>link:arm"]);
  });

  it("isolating leaves only the involved parts, and turning it off brings every part back", () => {
    draw({ isolate: true });
    expect(ids("[data-node]")).toEqual([...INV[0]!].sort());
    expect(el.querySelectorAll("path.gv-edge").length).toBe(2);
    draw({ isolate: false });
    expect(ids("[data-node]")).toHaveLength(GRAPH.nodes.length);
  });

  it("with no check chosen it shows everything and isolating changes nothing", () => {
    draw({ selected: null, isolate: true });
    expect(ids("[data-node]")).toHaveLength(GRAPH.nodes.length);
  });

  it("a click or Enter on a part with a 3D body picks it; a rail has none", () => {
    const onPick = vi.fn();
    draw({ onPick });
    tap("device:servo");
    expect(onPick).toHaveBeenLastCalledWith("servo");
    act(() => { el.querySelector('[data-node="joint:j1"]')!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); });
    expect(onPick).toHaveBeenLastCalledWith("j1");
    tap("rail:r5");
    expect(onPick).toHaveBeenCalledTimes(2);
  });

  it("a lit part the graph does not draw is named by kind and id, never as a raw dotted id", () => {
    draw({ cards: [card("x", ["rail:rail.5v", "device:servo"])], selected: 0 });
    expect(el.querySelector(".gv-uses")!.textContent).toBe("This check uses: rail rail.5v, Servo");
  });

  it("the screen-reader line names exactly the involved parts", () => {
    draw({ selected: 1 });
    expect(el.querySelector(".gv-uses")!.textContent).toBe("This check uses: 5 V rail, battery, Servo");
  });

  it("focus shows the part's name and the numbers the check reads from it, only if it is involved", () => {
    draw();
    act(() => (el.querySelector('[data-node="device:servo"]') as HTMLElement).dispatchEvent(new FocusEvent("focusin", { bubbles: true })));
    expect(el.querySelector(".gv-tip")!.textContent).toBe("Servo, device. This check reads 9.46 W.");
    act(() => (el.querySelector('[data-node="link:base"]') as HTMLElement).dispatchEvent(new FocusEvent("focusin", { bubbles: true })));
    expect(el.querySelector(".gv-tip")!.textContent).toBe("base, link");
  });

  it("the picker chooses a check", () => {
    const onSelectCard = vi.fn();
    draw({ onSelectCard });
    const sel = el.querySelector("select")!;
    act(() => { sel.value = "1"; sel.dispatchEvent(new Event("change", { bubbles: true })); });
    expect(onSelectCard).toHaveBeenCalledWith(1);
  });
});

describe("layout", () => {
  it("runs the structure tree left to right with the rest in columns after it", () => {
    const p = layout(GRAPH);
    expect(p.get("link:base")!.x).toBeLessThan(p.get("joint:j1")!.x);
    expect(p.get("joint:j1")!.x).toBeLessThan(p.get("link:arm")!.x);
    expect(p.get("device:servo")!.x).toBeGreaterThan(p.get("link:arm")!.x);
    expect(p.get("rail:r5")!.x).toBeGreaterThan(p.get("device:servo")!.x);
  });
  it("maps nodes to 3D parts: links, joints and devices only", () => {
    expect(isolateIds(["link:a", "rail:r", "joint:j", "device:d", "battery"])).toEqual(["a", "j", "d"]);
  });
});

// GRAPH-2: what a picked part is connected to, from the edges the payload gave.
const G2 = {
  nodes: [...GRAPH.nodes, n("rail:r3", "3 V rail"), n("device:cam", "Camera"), n("device:imu", "Imu"), n("link:tip", "tip"), n("joint:j2")],
  edges: [
    ...GRAPH.edges, { from: "rail:r5", to: "rail:r3", kind: "power" }, { from: "rail:r3", to: "device:cam", kind: "power" },
    { from: "device:cam", to: "link:tip", kind: "mounts" }, { from: "link:arm", to: "joint:j2", kind: "structure" }, { from: "joint:j2", to: "link:tip", kind: "structure" },
  ],
} as RobotGraph;
const lit = () => ids(".gv-node.is-lit");
const panel = () => el.querySelector(".gv-focus")?.textContent ?? "";
const press = (text: string) => act(() => { ([...el.querySelectorAll("button")].find((b) => b.textContent === text) as HTMLElement).click(); });

describe("what a picked part is connected to", () => {
  beforeEach(() => setOnlyParts([]));

  it("a rail lights exactly what it feeds, down the chain, and not what feeds it", () => {
    draw({ graph: G2, selected: null });
    tap("rail:r5");
    expect(lit()).toEqual(["device:cam", "device:servo", "rail:r3", "rail:r5"]);
    expect(panel()).toContain("Feeds: Servo, 3 V rail, Camera");
  });

  it("a joint shows its actuator, what it carries and the checks that read it", () => {
    draw({ graph: G2, selected: null });
    tap("joint:j1");
    expect(panel()).toContain("Driven by: Servo");
    expect(panel()).toContain("Carries: arm, j2, tip, Camera");
    expect(panel()).toContain("Servo too weak");
    expect(panel()).not.toContain("Rail overloaded");
  });

  it("a link in the list opens that check", () => {
    const onSelectCard = vi.fn();
    draw({ graph: G2, selected: null, onSelectCard });
    tap("rail:r5");
    press("Rail overloaded");
    expect(onSelectCard).toHaveBeenCalledWith(1);
  });

  it("Only these keeps the lit parts in the graph and sends them to the shared 3D isolate; again brings them back", () => {
    draw({ graph: G2, selected: null });
    tap("rail:r5");
    press("Only these");
    expect(ids("[data-node]")).toEqual(["device:cam", "device:servo", "rail:r3", "rail:r5"]);
    expect(getViewState().only.sort()).toEqual(["cam", "servo"]);
    press("Show everything");
    expect(getViewState().only).toEqual([]);
    expect(ids("[data-node]")).toHaveLength(G2.nodes.length);
  });

  it("a part picked in 3D lights its connections too", () => {
    draw({ graph: G2, selected: null, selectedPart: "j1" });
    expect(lit()).toContain("device:servo");
    expect(panel()).toContain("Driven by: Servo");
  });
});
