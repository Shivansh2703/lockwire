/**
 * E2.2 — the Design tab's selected part survives a reopen when the part is still there,
 * and the missing/stale-revision refusal at App.tsx:157/175 writes nothing.
 *
 * Selection is `selectionStore.ts`'s own key: `DesignPanel` ties it to
 * `${contract}:${scene.hash}` (selectionStore.ts:34-38) and clears it whenever that key
 * changes -- so a reopen that comes back with the SAME scene hash (the part unchanged)
 * keeps the id selected, and one that comes back with a DIFFERENT hash (the part gone,
 * or any other geometry change) clears it. Both are proven here by driving the real
 * edit -> reopen round trip (`DesignControls` -> `App.applyDesignEdit` ->
 * `App.replaceOpenedPage`), the one path a reopened payload actually takes in the app.
 */
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import type { PagePayload } from "../shared/api.gen";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import { getSelectedPartId, setSelectedPartId } from "./scene/selectionStore";

vi.mock("./scene/Canvas", () => ({
  Canvas: () => <div data-testid="scene-canvas" />,
  useScene: () => { throw new Error("unused in this test"); },
}));

function mockMatchMedia(): void {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as unknown as typeof window.matchMedia;
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  mockMatchMedia();
  setSelectedPartId(null);
  container = document.createElement("div");
  document.body.appendChild(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  Reflect.deleteProperty(window, "lockwire");
});

const picked = (opened: unknown) => ({ started: false, opened });

function clickOpenRobot() {
  const button = Array.from(container.querySelectorAll("button")).find((b) => b.textContent === "Open robot")!;
  act(() => button.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

function clickModeTab(name: string) {
  const button = Array.from(container.querySelectorAll('button[role="tab"]')).find((b) => b.textContent === name)!;
  act(() => button.dispatchEvent(new MouseEvent("click", { bubbles: true })));
}

const EMPTY_GRAPH = {
  off: { nodes: [], edges: [], label: "Off", caption: "" },
  mechanical: { nodes: [], edges: [], label: "Mechanical", caption: "" },
  power: { nodes: [], edges: [], label: "Power", caption: "" },
  data: { nodes: [], edges: [], label: "Data", caption: "" },
  health: { nodes: [], edges: [], label: "Health", caption: "" },
};

function scenePart(id: string) {
  return {
    id,
    node: id,
    name: id,
    state: "ok" as const,
    positions: { type: "i16" as const, data: "" },
    normals: null,
    indices: { type: "u16" as const, data: "" },
    color: [],
    translation: [],
    group: { id: "other", label: "Other", kind: "other" as const },
    lenses: ["off" as const],
  };
}

function page(overrides: { revision: string; hash: string; parts: ReturnType<typeof scenePart>[] }): PagePayload {
  return {
    revision: overrides.revision,
    robot: "Holybro X500 V2 PX4 reference quadrotor",
    contract_id: "x500",
    state: "go",
    headline: X500_CHECK_PAYLOAD.headline,
    qualifier: "",
    scored: X500_CHECK_PAYLOAD.scored,
    violations: 0,
    warnings: 0,
    unchecked: 0,
    adds_up: [],
    cards: X500_CHECK_PAYLOAD.cards,
    joints: [],
    links: [],
    graph: EMPTY_GRAPH,
    budgets: [],
    constraints: [],
    setup: [],
    run: null,
    scene: {
      source: "fixture.glb",
      hash: overrides.hash,
      parts: overrides.parts,
      labels: [],
      scene_only: [],
      contract_only: [],
      bounds: [],
      triangles: 0,
      vertices: 0,
      display_triangles: 0,
      honesty: "fixture",
    },
    modes: [],
    mode_labels: {},
    command_modes: {},
    health: null,
    health_devices: null,
    cli_line: "lockwire gui /tmp/x500/lockwire.yaml --out lockwire-gui.html",
    document_identity: "x500",
    faces: {
      design: { title: "Design", parts: [], cli: "lockwire check /tmp/x500/lockwire.yaml" },
      declare: { title: "Declare", lines: [] },
      plan: { title: "Check a plan before it runs", cli: "lockwire plan /tmp/x500/lockwire.yaml plan.yaml", outcomes: null },
      reference: { title: "Reference", drawers: [], devices: [], power_tree: null },
    },
  } as unknown as PagePayload; // the optional health extras are not part of the free fixture
}

const DECLARE_FORM = {
  revision: "revision",
  rows: [{ path: "mass.total", label: "Total mass", group: "Total", short: "Mass", unit: "kg", kind: "declared" as const, value: 2,
    provenance: { source: "weigh-in.csv", witness: "Maya", locked_at: "2026-09-09" } }],
  witness: { prefill: "Maya" },
};

/** Opens the lockfile, switches to Design, selects `partId`, then submits a design edit
 * whose reopen (the mocked SECOND `getContractPage` call) returns `nextPage` -- the one
 * round trip that actually replaces `opened.page` in the running app. */
async function openSelectEditAndReopen(
  writeContract: ReturnType<typeof vi.fn>,
  firstPage: PagePayload,
  nextPage: PagePayload,
  partId: string,
) {
  (window as unknown as { lockwire: object }).lockwire = {
    startFromUrdf: vi.fn().mockResolvedValue(picked({ opened: true, path: "/tmp/x500/lockwire.yaml", payload: X500_CHECK_PAYLOAD, checkPayload: X500_CHECK_PAYLOAD })),
    getContractPage: vi.fn().mockResolvedValueOnce(firstPage).mockResolvedValueOnce(nextPage),
    getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM),
    getHistory: vi.fn().mockResolvedValue([]),
    writeContract,
  };
  root = createRoot(container);
  act(() => root.render(<App />));
  clickOpenRobot();
  await act(async () => {});

  clickModeTab("Design");
  act(() => container.querySelectorAll<HTMLButtonElement>('.kit-tree-chev[aria-expanded="false"]').forEach((c) => c.click()));
  const partButton = container.querySelector(`button[data-part-id="${partId}"]`) as HTMLButtonElement;
  act(() => partButton.dispatchEvent(new MouseEvent("click", { bubbles: true })));
  expect(getSelectedPartId()).toBe(partId);

  const sourceInput = container.querySelector('input[aria-label="Source"]') as HTMLInputElement;
  const witnessInput = container.querySelector('input[aria-label="Witness"]') as HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
  act(() => {
    setter.call(sourceInput, "measurement-log");
    sourceInput.dispatchEvent(new Event("input", { bubbles: true }));
    setter.call(witnessInput, "Maya");
    witnessInput.dispatchEvent(new Event("input", { bubbles: true }));
  });
  const form = container.querySelector("form.design-controls") as HTMLFormElement;
  act(() => form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  await act(async () => {});
}

describe("E2.2 -- selection survives a reopen that keeps the part, and is cleared when it does not", () => {
  it("keeps the selected part id when the reopened scene is unchanged", async () => {
    const writeContract = vi.fn().mockResolvedValue({ payload: { written: true, revision: "revision" } });
    const firstPage = page({ revision: "revision", hash: "hash", parts: [scenePart("frame")] });
    const samePage = page({ revision: "revision", hash: "hash", parts: [scenePart("frame")] });
    await openSelectEditAndReopen(writeContract, firstPage, samePage, "frame");

    expect(getSelectedPartId()).toBe("frame");
  });

  it("clears the selected part id when the reopened scene no longer has it", async () => {
    const writeContract = vi.fn().mockResolvedValue({ payload: { written: true, revision: "revision" } });
    const firstPage = page({ revision: "revision", hash: "hash", parts: [scenePart("frame")] });
    const replacedPage = page({ revision: "revision", hash: "hash-2", parts: [scenePart("bracket")] });
    await openSelectEditAndReopen(writeContract, firstPage, replacedPage, "frame");

    expect(getSelectedPartId()).toBeNull();
  });
});

describe("E2.2 -- a stale/missing revision refuses an edit and writes nothing", () => {
  it("shows the reload refusal on Declare and never calls the write IPC for it", async () => {
    const writeContract = vi.fn().mockResolvedValue({ payload: { written: true, revision: "revision" } });
    const firstPage = page({ revision: "revision", hash: "hash", parts: [scenePart("frame")] });
    // The reopened page comes back with an empty revision -- `replaceOpenedPage` (App.tsx:291-311)
    // takes it verbatim (`nextPage.revision ?? current.revision`; "" is not nullish), so
    // `opened.revision` is now falsy exactly as it would be after a stale/expired read.
    const staleRevisionPage = page({ revision: "", hash: "hash", parts: [scenePart("frame")] });
    await openSelectEditAndReopen(writeContract, firstPage, staleRevisionPage, "frame");

    expect(writeContract).toHaveBeenCalledTimes(1); // only the design edit above

    clickModeTab("Declare");
    const valueInput = container.querySelector('input[aria-label="value"]') as HTMLInputElement;
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")!.set!;
    act(() => {
      setter.call(valueInput, "3");
      valueInput.dispatchEvent(new Event("input", { bubbles: true }));
    });
    const declareButton = Array.from(container.querySelectorAll(".declare-face button")).find((b) => b.textContent === "Declare")!;
    act(() => declareButton.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await act(async () => {});

    expect(container.querySelector(".kit-notice-error")?.textContent).toBe(
      "lockfile revision missing; reload before applying this edit",
    );
    expect(writeContract).toHaveBeenCalledTimes(1); // still just the design edit -- Declare wrote nothing
  });
});
