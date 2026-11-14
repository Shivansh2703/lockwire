// Audit app-bugs scenarios (0.5.0). Each `it` asserts the correct behaviour; the box that fixes it is named on its describe.
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { DECLARE_FORM, PAGE, deferred, goMode, mockMatchMedia, picked } from "./App.testkit";
import type { PagePayload } from "../shared/api.gen";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import type { CheckFacePayload } from "./checkFacePayload";
import { setSelectedPartId } from "./scene/selectionStore";

vi.mock("./scene/Canvas", () => ({
  Canvas: ({ children }: { children?: React.ReactNode }) => <div data-testid="scene-canvas">{children}</div>,
  useScene: () => ({ meshes: [] }),
}));

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

const NOGO: CheckFacePayload = { ...X500_CHECK_PAYLOAD, verdict: "NO-GO", robot: "Robot A" };
const doc = (path: string, payload: CheckFacePayload = NOGO) => ({ opened: true, path, payload, checkPayload: { ...payload, scene: null } });
const word = () => container.querySelector(".check-verdict-word")?.textContent;
const button = (t: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === t);

function mount(api: Record<string, unknown>) {
  Object.assign(window, { lockwire: {
    getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]),
    checkPlan: vi.fn().mockResolvedValue(null), ...api,
  } });
  root = createRoot(container);
  act(() => root.render(<App />));
}

// APP-2.
describe("Design edits refresh the whole Check state", () => {
  it("after Cmd+Z in Design clears every finding, the Check verdict follows the new page", async () => {
    const fixedPage = { ...PAGE, revision: "r2", state: "go", violations: 0, warnings: 0, cards: [], headline: "All good" } as PagePayload;
    const getContractPage = vi.fn().mockResolvedValueOnce({ ...PAGE, revision: "r1", state: "stop", violations: 1 }).mockResolvedValue(fixedPage);
    const openLockfile = vi.fn().mockResolvedValue(doc("/a.yaml", { ...NOGO, verdict: "GO", violations: 0, warnings: 0, cards: [] }));
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage, openLockfile, writeContract: vi.fn().mockResolvedValue({ payload: { undone: true } }) });
    await act(async () => button("Open robot")!.click());
    expect(word()).toBe("NO-GO");
    goMode(container, "Design");
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "z", metaKey: true, bubbles: true })); });
    expect(getContractPage).toHaveBeenCalledTimes(2); // one page read per refresh, not two
    goMode(container, "Check");
    expect(container.querySelector(".check-headline")?.textContent).toContain("0 findings");
    expect(word()).not.toBe("NO-GO"); // was the bug: a page-only merge never refreshed the verdict word
  });
});

// APP-1.
describe("late replies for the previous robot", () => {
  it("a Design edit on robot A whose write returns after robot B is opened never reloads A over B", async () => {
    let menuCb: ((r: unknown) => void) | undefined;
    const write = deferred<unknown>();
    const getContractPage = vi.fn(({ contract }: { contract: string }) => Promise.resolve({ ...PAGE, revision: contract, robot: contract === "/b.yaml" ? "Robot B" : "Robot A" }));
    const openLockfile = vi.fn(({ contract }: { contract: string }) => Promise.resolve(doc(contract)));
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage, openLockfile, writeContract: vi.fn(() => write.promise),
      onOpenedFromMenu: (cb: (r: unknown) => void) => { menuCb = cb; return () => undefined; } });
    await act(async () => button("Open robot")!.click());
    Reflect.deleteProperty(window.lockwire, "onOpenedFromMenu"); // no menu accelerator: the page's own Cmd+Z runs the edit (APP-3)
    goMode(container, "Design");
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "z", metaKey: true, bubbles: true })); }); // undo on A in flight
    await act(async () => menuCb!({ result: picked(doc("/b.yaml")) })); // File > Open Recent > robot B
    expect(container.textContent).toContain("Robot B");
    await act(async () => write.resolve({ payload: { undone: true } }));
    expect(openLockfile).not.toHaveBeenCalled(); // was the bug: A's refresh replaced B
    expect(container.textContent).not.toContain("Robot A");
  });

  it.skip("robot A's history reply landing after robot B is open is not shown for B", async () => {
    const histA = deferred<unknown>();
    const getHistory = vi.fn(({ contract }: { contract: string }) => contract === "/a.yaml" ? histA.promise
      : Promise.resolve([{ sha: "bbb", author: "AuthorBee", date: "2026-11-01", message: "B only", verdict: "GO", fact_diff: null }]));
    const openLockfile = vi.fn().mockResolvedValue(doc("/b.yaml", { ...NOGO, robot: "Robot B" }));
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage: ({ contract }: { contract: string }) => Promise.resolve({ ...PAGE, revision: contract }), openLockfile, getHistory });
    await act(async () => button("Open robot")!.click());
    await act(async () => button("Re-check")!.click());
    await act(async () => histA.resolve([{ sha: "aaa", author: "AuthorAay", date: "2026-10-01", message: "A only commit", verdict: "GO", fact_diff: null }]));
    goMode(container, "Docs");
    act(() => [...container.querySelectorAll("label.kit-seg-opt")].find((b) => b.textContent === "History")!.querySelector("input")!.click());
    expect(container.textContent).not.toContain("AuthorAay"); // was the bug: no load guard on getHistory
    expect(container.textContent).toContain("AuthorBee");
  });
});

// APP-3.
describe("Edit > Undo from the menu", () => {
  const tabSel = () => [...container.querySelectorAll('[role="tab"][aria-selected="true"]')].map((t) => t.textContent);
  it("keeps the user on the Design tab, keeps the missing-meshes panel, and refreshes once", async () => {
    let menuCb: ((r: unknown) => void) | undefined;
    const startFromUrdf = vi.fn().mockResolvedValue({ started: true, urdf: "/r/a.urdf", opened: doc("/a.yaml"),
      start: { path: "/a.yaml", revision: "r", written: true, mesh_gap: { missing: 4, total: 4, example: "package://p/b.stl" } } });
    const openLockfile = vi.fn().mockResolvedValue(doc("/a.yaml"));
    mount({ startFromUrdf, openLockfile, getContractPage: () => Promise.resolve({ ...PAGE, revision: "r" }),
      onOpenedFromMenu: (cb: (r: unknown) => void) => { menuCb = cb; return () => undefined; } });
    await act(async () => button("Open robot")!.click());
    goMode(container, "Design");
    await act(async () => menuCb!({ stepped: "/a.yaml" })); // what main's stepFromMenu pushes after undo-edit
    expect(tabSel()).toContain("Design"); // was the bug: the reopen set mode Check
    expect(openLockfile.mock.calls).toEqual([[{ contract: "/a.yaml" }]]); // the guarded reload, not a fresh start
    expect(startFromUrdf).toHaveBeenCalledTimes(1);
    goMode(container, "Check");
    expect(container.querySelector(".empty-geometry-panel")).not.toBeNull(); // was the bug: the reopen dropped it
  });

  it("in the app, Cmd+Z in Design is left to the menu, so one keypress undoes once", async () => {
    const writeContract = vi.fn().mockResolvedValue({ payload: { undone: true } });
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage: () => Promise.resolve({ ...PAGE, revision: "r" }), writeContract,
      onOpenedFromMenu: () => () => undefined });
    await act(async () => button("Open robot")!.click());
    goMode(container, "Design");
    await act(async () => { window.dispatchEvent(new KeyboardEvent("keydown", { key: "z", metaKey: true, bubbles: true })); });
    expect(writeContract).not.toHaveBeenCalled(); // the menu's accelerator runs the one undo
  });
});

// APP-4.
describe("Declare tab write", () => {
  it("re-checks after a value is declared, and the row shows the new value", async () => {
    const getContractPage = vi.fn().mockResolvedValue({ ...PAGE, revision: "r1" });
    const openLockfile = vi.fn().mockResolvedValue(doc("/a.yaml"));
    const writeContract = vi.fn().mockResolvedValue({ payload: { written: true, revision: "r2" } });
    const row = DECLARE_FORM.rows[0];
    const getDeclareForm = vi.fn().mockResolvedValueOnce(DECLARE_FORM).mockResolvedValue({ ...DECLARE_FORM, revision: "r2", rows: [{ ...row, value: 3.75 }] });
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage, openLockfile, writeContract, getDeclareForm });
    await act(async () => button("Open robot")!.click());
    goMode(container, "Declare");
    expect(container.querySelector(".declare-face")?.textContent).not.toContain("3.75");
    await act(async () => container.querySelector<HTMLButtonElement>(".declare-face button")!.click());
    expect(writeContract).toHaveBeenCalledWith(expect.objectContaining({ command: "declare" }));
    expect(openLockfile).toHaveBeenCalledWith({ contract: "/a.yaml" }); // was the bug: only the revision was bumped
    expect(container.querySelector(".declare-face")?.textContent).toContain("3.75");
  });
});

// APP-1.
describe("an edit's reload after the robot changed", () => {
  it("a declare on robot A that returns after robot B is open never gives B A's revision", async () => {
    let menuCb: ((r: unknown) => void) | undefined;
    const write = deferred<unknown>();
    const writeContract = vi.fn().mockReturnValueOnce(write.promise).mockResolvedValue({ payload: { written: true, revision: "rB2" } });
    const getContractPage = vi.fn(({ contract }: { contract: string }) => Promise.resolve({ ...PAGE, revision: `rev${contract}`, robot: contract === "/b.yaml" ? "Robot B" : "Robot A" }));
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage, writeContract,
      onOpenedFromMenu: (cb: (r: unknown) => void) => { menuCb = cb; return () => undefined; } });
    await act(async () => button("Open robot")!.click());
    goMode(container, "Declare");
    await act(async () => container.querySelector<HTMLButtonElement>(".declare-face button")!.click()); // declare on A in flight
    await act(async () => menuCb!({ result: picked(doc("/b.yaml")) })); // File > Open Recent > robot B
    await act(async () => write.resolve({ payload: { written: true, revision: "rA2" } }));
    goMode(container, "Declare"); // opening B went back to Check
    await act(async () => container.querySelector<HTMLButtonElement>(".declare-face button")!.click()); // declare on B
    expect(writeContract).toHaveBeenLastCalledWith(expect.objectContaining({ args: expect.objectContaining({ contract: "/b.yaml", expected_revision: "rev/b.yaml" }) }));
  });

  it("a slow write on robot A does not reopen A over robot B", async () => {
    let menuCb: ((r: unknown) => void) | undefined;
    const write = deferred<unknown>();
    const getContractPage = vi.fn(({ contract }: { contract: string }) => Promise.resolve({ ...PAGE, revision: contract, robot: contract === "/b.yaml" ? "Robot B" : "Robot A" }));
    const openLockfile = vi.fn(({ contract }: { contract: string }) => Promise.resolve(doc(contract)));
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage, openLockfile, writeContract: vi.fn(() => write.promise),
      onOpenedFromMenu: (cb: (r: unknown) => void) => { menuCb = cb; return () => undefined; } });
    await act(async () => button("Open robot")!.click());
    act(() => button("Compare with datasheet")!.click());
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    for (const [label, v] of [["datasheet weight", "1.5"], ["datasheet source", "maker page"]]) {
      const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
      act(() => { setter.call(input, v); input.dispatchEvent(new Event("input", { bubbles: true })); });
    }
    await act(async () => button("Compare")!.click()); // write on A now in flight
    await act(async () => menuCb!({ result: picked(doc("/b.yaml")) })); // File > Open Recent > robot B
    expect(container.textContent).toContain("Robot B");
    await act(async () => write.resolve({ payload: { written: true, revision: "rA2" } }));
    expect(container.textContent).toContain("Robot B"); // was the bug: compareMass reopens the captured opened.path (A)
    expect(container.textContent).not.toContain("Robot A");
  });

  it("a write on robot A that returns while robot B is still opening does not cancel B", async () => {
    let menuCb: ((r: unknown) => void) | undefined;
    const write = deferred<unknown>();
    const pageB = deferred<PagePayload>();
    const getContractPage = vi.fn(({ contract }: { contract: string }) => contract === "/b.yaml" ? pageB.promise : Promise.resolve({ ...PAGE, revision: "rA", robot: "Robot A" }));
    const openLockfile = vi.fn(({ contract }: { contract: string }) => Promise.resolve(doc(contract)));
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage, openLockfile, writeContract: vi.fn(() => write.promise),
      onOpenedFromMenu: (cb: (r: unknown) => void) => { menuCb = cb; return () => undefined; } });
    await act(async () => button("Open robot")!.click());
    act(() => button("Compare with datasheet")!.click());
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
    for (const [label, v] of [["datasheet weight", "1.5"], ["datasheet source", "maker page"]]) {
      const input = container.querySelector<HTMLInputElement>(`[aria-label="${label}"]`)!;
      act(() => { setter.call(input, v); input.dispatchEvent(new Event("input", { bubbles: true })); });
    }
    await act(async () => button("Compare")!.click()); // write on A now in flight
    await act(async () => menuCb!({ result: picked(doc("/b.yaml")) })); // robot B's page is still being built
    await act(async () => write.resolve({ payload: { written: true, revision: "rA2" } }));
    expect(openLockfile).not.toHaveBeenCalled(); // A's reload would cancel B's open
    await act(async () => pageB.resolve({ ...PAGE, revision: "rB", robot: "Robot B" }));
    expect(container.textContent).toContain("Robot B");
  });
});

describe("late history replies after a robot switch (APP-BUG5)", () => {
  const HIST = (who: string) => [{ sha: "aaa", author: who, date: "2026-10-01", message: `${who} commit`, verdict: "GO", fact_diff: null }];
  const openAThenB = async (api: Record<string, unknown>) => {
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage: ({ contract }: { contract: string }) => Promise.resolve({ ...PAGE, revision: contract }),
      openLockfile: vi.fn().mockResolvedValue(doc("/b.yaml", { ...NOGO, robot: "Robot B" })), ...api });
    await act(async () => button("Open robot")!.click());
    await act(async () => button("Re-check")!.click());
  };
  const showHistory = () => { goMode(container, "Docs"); act(() => [...container.querySelectorAll("label.kit-seg-opt")].find((b) => b.textContent === "History")!.querySelector("input")!.click()); };

  it("robot A's history reply landing after robot B is open is not shown for B", async () => {
    const histA = deferred<unknown>();
    await openAThenB({ getHistory: ({ contract }: { contract: string }) => contract === "/a.yaml" ? histA.promise : Promise.resolve(HIST("AuthorBee")) });
    await act(async () => histA.resolve(HIST("AuthorAay")));
    showHistory();
    expect(container.textContent).not.toContain("AuthorAay");
    expect(container.textContent).toContain("AuthorBee");
  });

  it("a failed history reply for robot A does not mark B's history unavailable", async () => {
    let fail: (e: Error) => void = () => undefined;
    const histA = new Promise<unknown>((_, rej) => { fail = rej; });
    await openAThenB({ getHistory: ({ contract }: { contract: string }) => contract === "/a.yaml" ? histA : Promise.resolve(HIST("AuthorBee")) });
    await act(async () => fail(new Error("git gone")));
    showHistory();
    expect(container.textContent).toContain("AuthorBee");
  });

  it("robot B's history reply still lands when a newer open fails while it is pending", async () => {
    const histB = deferred<unknown>();
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/b.yaml"))), openLockfile: vi.fn().mockRejectedValue(new Error("unreadable")), getContractPage: ({ contract }: { contract: string }) => Promise.resolve({ ...PAGE, revision: contract }), getHistory: () => histB.promise });
    await act(async () => button("Open robot")!.click());
    await act(async () => button("Re-check")!.click()); // a newer open fails; B stays on screen
    await act(async () => histB.resolve(HIST("AuthorBee")));
    showHistory();
    expect(container.textContent).toContain("AuthorBee");
  });

  it("a compare reply landing after robot B is open is not shown for B", async () => {
    const diffA = deferred<unknown>();
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage: ({ contract }: { contract: string }) => Promise.resolve({ ...PAGE, revision: contract }),
      openLockfile: vi.fn().mockResolvedValue(doc("/b.yaml", { ...NOGO, robot: "Robot B" })), getHistory: vi.fn().mockResolvedValue(HIST("AuthorAay")), diffContract: vi.fn().mockReturnValue(diffA.promise) });
    await act(async () => button("Open robot")!.click());
    showHistory();
    act(() => container.querySelector<HTMLButtonElement>(".history-diff-disk")!.click()); // compare sent while A is open
    goMode(container, "Check");
    await act(async () => button("Re-check")!.click()); // B replaces A
    showHistory();
    await act(async () => diffA.resolve({ changed: true, changes: [], added: [{ kind: "part", id: "STALE_PART" }], removed: [], findings_new: [], findings_gone: [] }));
    expect(container.textContent).not.toContain("STALE_PART");
  });
});

// AUD-SHELL: File > Close robot.
describe("File > Close robot", () => {
  let close: (() => void) | undefined;
  const onCloseRobot = (cb: () => void) => { close = cb; return () => undefined; };
  const starters = () => vi.fn().mockResolvedValue([{ name: "arm", title: "Arm", based_on: "x" }]);

  it("returns to the start card with its examples", async () => {
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage: vi.fn().mockResolvedValue(PAGE), getStarters: starters(), onCloseRobot });
    await act(async () => button("Open robot")!.click());
    expect(word()).toBe("NO-GO");
    expect(container.querySelector(".open-face")).toBeNull();
    await act(async () => close!());
    expect(container.querySelector(".open-face")).not.toBeNull();
    expect(container.textContent).toContain("OR TRY AN EXAMPLE");
    expect(word()).toBeUndefined();
  });

  it("a Re-check reply that lands after Close does not bring the robot back", async () => {
    const late = deferred<ReturnType<typeof doc>>();
    mount({ startFromUrdf: () => Promise.resolve(picked(doc("/a.yaml"))), getContractPage: vi.fn().mockResolvedValue(PAGE), openLockfile: () => late.promise, getStarters: starters(), onCloseRobot });
    await act(async () => button("Open robot")!.click());
    await act(async () => button("Re-check")!.click()); // a load in flight
    await act(async () => close!());
    await act(async () => late.resolve(doc("/a.yaml")));
    expect(container.querySelector(".open-face")).not.toBeNull();
    expect(word()).toBeUndefined();
  });

  it("the start card after Close carries nothing of the closed robot (its example notice)", async () => {
    const newRobot = vi.fn().mockResolvedValue({ started: false, example: "/ud/examples/arm", opened: doc("/ud/examples/arm/lockwire.yaml") });
    mount({ newRobot, getContractPage: vi.fn().mockResolvedValue(PAGE), getStarters: starters(), onCloseRobot,
      getSidecarState: vi.fn().mockResolvedValue({ phase: "ready" }), onSidecarState: () => () => undefined });
    await act(async () => {});
    await act(async () => [...container.querySelectorAll<HTMLButtonElement>(".open-face-new button")].find((b) => b.textContent!.startsWith("Arm ·"))!.click());
    expect(container.textContent).toContain("This is an example.");
    await act(async () => close!());
    expect(container.querySelector(".open-face")).not.toBeNull();
    expect(container.textContent).not.toContain("This is an example.");
  });
});

describe("start card when the engine failed to start", () => {
  it("says the engine is not running, shows its error, and keeps Open robot disabled", async () => {
    mount({ getSidecarState: vi.fn().mockResolvedValue({ phase: "error", message: "port in use" }), onSidecarState: () => () => undefined, getStarters: vi.fn().mockResolvedValue([]) });
    await act(async () => {});
    expect(container.textContent).toContain("could not start its engine: port in use");
    expect(container.textContent).toContain("The engine is not running.");
    expect(button("Open robot")!.disabled).toBe(true);
  });
});
