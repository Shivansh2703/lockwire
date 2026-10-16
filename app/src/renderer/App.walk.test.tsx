import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { mockMatchMedia, picked } from "./App.testkit";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import { getSelectedGroup, getSelectedPartId, setSelectedPartId } from "./scene/selectionStore";
import { getDesignView, resetDesignView, setDesignView } from "./scene/designView";
import { applyHighlight, focusCamera } from "./scene/robotScene";
import ANYMAL from "./anymal.click.fixture.json";
import X500 from "./x500.click.fixture.json";
import SCENE_DOC from "./scene/fixtures/x500-check-payload.json";

// Every button in the five tabs must do something: change the tab, the selection, what is open,
// the Declare row, the robot's light or camera, or ask the sidecar. A button that changes nothing is a false promise.
vi.mock("./scene/Canvas", () => ({
  Canvas: ({ children }: { children?: React.ReactNode }) => <div data-testid="scene-canvas">{children}</div>,
  useScene: () => ({}),
}));
vi.mock("./scene/robotScene", async (orig) => ({ ...(await orig<typeof import("./scene/robotScene")>()), applyHighlight: vi.fn(), applyDim: vi.fn(), focusCamera: vi.fn() }));

type Robot = { page: unknown; form: unknown; plan: unknown; part: string };
const ROBOTS: Record<"X500" | "ANYmal", Robot> = {
  X500: { page: { ...X500.page, scene: SCENE_DOC.scene }, form: X500.form, plan: X500.plan, part: "dev.camera.oak-d-lite" },
  ANYmal: { page: ANYMAL.page, form: ANYMAL.form, plan: X500.plan, part: (ANYMAL.page.joints as { name: string }[])[0]!.name },
};
let robot: Robot = ROBOTS.X500;
const HISTORY = [{ sha: "abc1234", author: "Maya", date: "2026-09-09", message: "Re-lock", verdict: "NO-GO", fact_diff: { changes: [{ path: "topology/rails/rail.pm02-5v/capacity", old: "15 W", new: "15.6 W" }] } }];

let container: HTMLDivElement;
let root: Root | null = null;
let calls = 0;
beforeEach(() => { mockMatchMedia(); setSelectedPartId(null); resetDesignView(); container = document.createElement("div"); document.body.appendChild(container); });
afterEach(() => { if (root) act(() => root!.unmount()); root = null; container.remove(); Reflect.deleteProperty(window, "lockwire"); });

const replies = (): Record<string, unknown> => ({
  startFromUrdf: picked({ opened: true, path: "/tmp/robot/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD }),
  getContractPage: robot.page, getDeclareForm: robot.form, getHistory: HISTORY, choosePlan: "/tmp/plan.yaml", checkPlan: robot.plan,
  diffContract: { changed: false, changes: [], added: [], removed: [], findings_new: [], findings_gone: [] },
});
const click = (el: Element) => act(() => (el as HTMLElement).click());

/** Fresh app, opened on the X500, on `tab`, with the first finding (or part) open. */
async function setup(tab: string, prep?: () => Promise<void> | void) {
  if (root) act(() => root!.unmount());
  container.innerHTML = "";
  setSelectedPartId(null);
  (window as unknown as { lockwire: object }).lockwire = new Proxy({}, {
    get: (_, name: string) => /^on[A-Z]/.test(name) ? () => () => {} // event subscriptions hand back an unsubscribe
      : () => { calls += 1; return Promise.resolve(replies()[name] ?? {}); },
  });
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: () => { calls += 1; return Promise.resolve(); } } });
  root = createRoot(container);
  act(() => root!.render(<App />));
  await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Open robot")!.click());
  await act(async () => {});
  click([...container.querySelectorAll('[role="tab"]')].find((t) => t.textContent === tab)!);
  await prep?.();
}
const state = () => JSON.stringify([document.documentElement.outerHTML, getSelectedPartId(), getSelectedGroup(), getDesignView(), calls, vi.mocked(focusCamera).mock.calls.length, vi.mocked(applyHighlight).mock.calls.length]);
// The active tab and an already-pressed selection row only confirm what is shown; they are skipped.
const walkable = () => [...container.querySelectorAll<HTMLButtonElement>("button")].filter((b) => !b.disabled && b.getAttribute("role") !== "tab" && !(b.classList.contains("kit-tree-row") && b.getAttribute("aria-pressed") === "true"));
const name = (b: HTMLButtonElement) => `${b.className || "button"} "${(b.getAttribute("aria-label") ?? b.textContent ?? "").slice(0, 60)}"`;

async function deadButtons(tab: string, prep?: () => Promise<void> | void): Promise<string[]> {
  await setup(tab, prep);
  const n = walkable().length;
  expect(n).toBeGreaterThan(3);
  const dead: string[] = [];
  for (let i = 0; i < n; i++) {
    await setup(tab, prep);
    const b = walkable()[i];
    if (!b) continue;
    const before = state();
    await act(async () => b.click());
    await act(async () => {});
    if (state() === before) dead.push(name(b));
  }
  return dead;
}

const openFirst = (sel: string) => () => { const row = container.querySelector(sel); if (row) click(row); };

describe.each(["X500", "ANYmal"] as const)("every button in the five tabs does something (%s)", (which) => {
  beforeEach(() => { robot = ROBOTS[which]; });
  it("Check", async () => expect(await deadButtons("Check", openFirst(".finding-card .kit-finding"))).toEqual([]), 60_000);
  it("Plan", async () => expect(await deadButtons("Plan", async () => {
    await act(async () => container.querySelector<HTMLButtonElement>(".kit-filepick button")!.click());
    await act(async () => {});
    openFirst(".plan-finding .kit-finding")();
  })).toEqual([]), 60_000);
  it("Design", async () => expect(await deadButtons("Design", () => act(() => setSelectedPartId(robot.part)))).toEqual([]), 60_000);
  it.runIf(which === "X500")("Design · Power", async () => expect(await deadButtons("Design", () => act(() => setDesignView({ lens: "power" })))).toEqual([]), 60_000);
  it("Declare", async () => expect(await deadButtons("Declare")).toEqual([]), 60_000);
  it("Docs", async () => expect(await deadButtons("Docs")).toEqual([]), 60_000);
});
