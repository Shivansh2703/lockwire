import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { PAGE_WITH_SCENE, PLAN, mockMatchMedia, picked } from "./App.testkit";
import type { PagePayload, PlanPayload } from "../shared/api.gen";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import { applyHighlight } from "./scene/robotScene";
import { getSelectedPartId, setSelectedPartId } from "./scene/selectionStore";

// The named things in the five tabs act. The canvas stub draws one pick button per part
// so a "3D pick" can be clicked like the real one (store first, then onPick).
vi.mock("./scene/Canvas", async () => {
  const store = await vi.importActual<typeof import("./scene/selectionStore")>("./scene/selectionStore");
  return {
    Canvas: ({ children, parts = [], onPick }: { children?: React.ReactNode; parts?: { id: string }[]; onPick?: (id: string) => void }) => (
      <div data-testid="scene-canvas">
        {parts.map((p) => <button key={p.id} type="button" data-pick={p.id} onClick={() => { store.setSelectedPartId(p.id); onPick?.(p.id); }} />)}
        {children}
      </div>
    ),
    useScene: () => ({}),
  };
});
vi.mock("./scene/robotScene", async (orig) => ({ ...(await orig<typeof import("./scene/robotScene")>()), applyHighlight: vi.fn(), applyDim: vi.fn(), focusCamera: vi.fn() }));

const part = (id: string, label: string) => ({ ...(PAGE_WITH_SCENE.scene as { parts: object[] }).parts[0], id, node: id, name: label, group: { id, label, kind: "device" } });
const CARD = {
  ...X500_CHECK_PAYLOAD.cards[0]!,
  severity: "violation" as const, shape: "power/rail", title: "Rail 5 V overloaded", quantity: "budgets/bud.power.5v", owner: "power-integration",
  observed: { value: 9, unit: "W", kind: "derived" as const, by: "x" }, whole: false,
  // The engine's real move sentence names the device first; the typed `rail` is where the click goes.
  alternatives: [{ text: "move 'dev.cam' to rail 'rail.telem' (2.60 W free); clears the overload", value: 2.6, unit: "W", kind: "derived-by-check_budgets", rail: "rail.telem", device: "dev.cam" }],
  instead: [{ text: "Move Camera to Telem rail (2.60 W free), clears the overload", kind: "derived", rail: "rail.telem", device: "dev.cam" }],
  targets: [{ kind: "rail" as const, id: "rail.5v", label: "Rail 5 V", parts: ["rail.5v", "dev.cam"] }, { kind: "device" as const, id: "dev.cam", label: "Camera", parts: ["dev.cam"] }],
};
const PAGE = {
  ...PAGE_WITH_SCENE,
  cards: [CARD],
  scene: { ...(PAGE_WITH_SCENE.scene as object), parts: [part("dev.cam", "Camera"), part("rail.5v", "Rail 5 V"), part("rail.telem", "Telem")], labels: [{ part: "dev.cam", title: "Rail 5 V overloaded" }] },
  faces: { ...PAGE_WITH_SCENE.faces, reference: { ...PAGE_WITH_SCENE.faces.reference, drawers: [{ title: "Sheets", provenance: "", cli_line: "lockwire describe", content: "| rail | fed from |\n| --- | --- |\n| Rail 5 V | Camera |" }] } },
} as unknown as PagePayload;
const row = (path: string, group: string, short: string) => ({ path, label: short, group, short, unit: "W", kind: "declared" as const, value: 1, provenance: null });
const FORM = { revision: "revision", rows: [row("budgets/bud.power.5v/limit", "Power 5 V", "limit"), row("devices/dev.cam/power/draw", "Camera", "draw")], witness: { prefill: "Maya" } };

let container: HTMLDivElement;
let root: Root;
beforeEach(() => { mockMatchMedia(); setSelectedPartId(null); container = document.createElement("div"); document.body.appendChild(container); });
afterEach(() => { act(() => root.unmount()); container.remove(); Reflect.deleteProperty(window, "lockwire"); });

async function open(plan: PlanPayload = PLAN) {
  (window as unknown as { lockwire: object }).lockwire = {
    startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: true, path: "/tmp/r.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD })),
    getContractPage: vi.fn().mockResolvedValue(PAGE), getDeclareForm: vi.fn().mockResolvedValue(FORM), getHistory: vi.fn().mockResolvedValue([]),
    choosePlan: vi.fn().mockResolvedValue("/tmp/plan.yaml"), checkPlan: vi.fn().mockResolvedValue(plan),
  };
  root = createRoot(container);
  act(() => root.render(<App />));
  await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Open robot")!.click());
  await act(async () => {});
}
const click = (el: Element | null | undefined) => act(() => (el as HTMLElement).click());
const byLabel = (label: string) => container.querySelector(`[aria-label="${label}"]`);
const byTitle = (title: string) => container.querySelector(`.kit-link[title="${title}"]`);
const tab = (name: string) => click([...container.querySelectorAll('[role="tab"]')].find((t) => t.textContent === name));
const activeTab = () => container.querySelector('[role="tab"][aria-selected="true"]')?.textContent;
const declared = () => container.querySelector(".declare-values .kit-tree-row.is-selected")?.getAttribute("data-part-id");

describe("click-through", () => {
  it("Check: a part chip lights that part; the same chip again opens it in Design", async () => {
    await open();
    click(byLabel("Show Camera"));
    expect(vi.mocked(applyHighlight).mock.lastCall?.[2]).toEqual(["dev.cam"]);
    expect(activeTab()).toBe("Check");
    click(byLabel("Show Camera"));
    expect(activeTab()).toBe("Design");
    expect(getSelectedPartId()).toBe("dev.cam");
  });

  it("Check: the observed number opens its quantity's Declare row", async () => {
    await open();
    click(container.querySelector(".kit-finding"));
    click(byTitle("Go to budgets/bud.power.5v"));
    expect(activeTab()).toBe("Declare");
    expect(declared()).toBe("budgets/bud.power.5v/limit");
  });

  it("Check: an Instead move selects the rail it names, not the device it moves", async () => {
    await open();
    click(container.querySelector(".kit-finding"));
    click([...container.querySelectorAll(".check-detail .kit-link")].find((b) => b.textContent?.startsWith("Move Camera")));
    expect(getSelectedPartId()).toBe("rail.telem");
  });

  it("Plan: the observed text opens the finding's Declare row", async () => {
    await open({ ...PLAN, verdict: "NO-GO", findings: [{ severity: "violation", quantity: "devices/dev.cam/power/draw", observed: "6 W", expected: "<= 5 W", action: "lower it" }] } as PlanPayload);
    tab("Plan");
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    click(container.querySelector(".plan-finding .kit-finding"));
    click([...container.querySelectorAll(".plan-finding .kit-link")].find((b) => b.textContent === "6 W"));
    expect(activeTab()).toBe("Declare");
    expect(declared()).toBe("devices/dev.cam/power/draw");
  });

  it("Design: the Findings count opens the card about that part in Check", async () => {
    await open();
    tab("Design");
    click(container.querySelector('[data-pick="dev.cam"]'));
    click(byTitle("Open its finding in Check"));
    expect(activeTab()).toBe("Check");
    expect(container.querySelector(".kit-finding.is-selected")?.textContent).toContain("Rail 5 V overloaded");
  });

  it("Declare: picking a part on the robot selects its row", async () => {
    await open();
    tab("Declare");
    expect(declared()).toBe("budgets/bud.power.5v/limit");
    click(container.querySelector('[data-pick="dev.cam"]'));
    expect(declared()).toBe("devices/dev.cam/power/draw");
  });

  it("Docs: a sheet cell naming a part lights it", async () => {
    await open();
    tab("Docs");
    click(byTitle("Show Camera"));
    expect(getSelectedPartId()).toBe("dev.cam");
    expect(activeTab()).toBe("Docs");
  });
});
