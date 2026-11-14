// An edit that lands during a Re-check of the same robot never leaves the old revision's verdict shown as current.
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { DECLARE_FORM, PAGE, deferred, goMode, mockMatchMedia, picked } from "./App.testkit";
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

const verdict = (v: "GO" | "NO-GO"): CheckFacePayload => ({ ...X500_CHECK_PAYLOAD, verdict: v, robot: "Robot A" });
const doc = (revision: string, v: "GO" | "NO-GO") => ({ opened: true, path: "/a.yaml", payload: verdict(v), checkPayload: { ...verdict(v), scene: null, revision } });
const word = () => container.querySelector(".check-verdict-word")?.textContent;
const button = (t: string) => [...container.querySelectorAll("button")].find((b) => b.textContent === t);

/** The file's revision moves on every write; the page always reads the file as it is now. */
function mount(openLockfile: ReturnType<typeof vi.fn>, start = "r0", extra: object = {}) {
  const file = { rev: start };
  const writeContract = vi.fn(async () => { file.rev = `${file.rev}+`; return { payload: { written: true, revision: file.rev } }; });
  const getContractPage = vi.fn(() => Promise.resolve({ ...PAGE, revision: file.rev }));
  Object.assign(window, { lockwire: {
    getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]), checkPlan: vi.fn().mockResolvedValue(null),
    startFromUrdf: () => Promise.resolve(picked(doc(start, "GO"))), getContractPage, openLockfile, writeContract, ...extra,
  } });
  root = createRoot(container);
  act(() => root.render(<App />));
  return file;
}
const declare = async () => { goMode(container, "Declare"); await act(async () => container.querySelector<HTMLButtonElement>(".declare-face button")!.click()); };

describe("an edit during Re-check of the same robot", () => {
  it("two edits during one Re-check: the old revision's verdict is never shown, and it re-checks once", async () => {
    const first = deferred<unknown>();
    const openLockfile = vi.fn().mockReturnValueOnce(first.promise).mockImplementation(() => Promise.resolve(doc(file.rev, "GO")));
    const file = mount(openLockfile);
    await act(async () => button("Open robot")!.click());
    expect(word()).toBe("GO");
    await act(async () => button("Re-check")!.click()); // the check reads r0
    await declare(); await declare(); // the file is now r0++, each edit's own reload skipped while the check runs
    await act(async () => first.resolve(doc("r0", "NO-GO")));
    expect(openLockfile).toHaveBeenCalledTimes(2); // was the bug: one call, r0's verdict shown as current
    goMode(container, "Check");
    expect(word()).toBe("GO");
    expect(container.textContent).not.toContain("changed while it was being checked");
  });

  it("one edit during one Re-check: the old revision's verdict is never shown, and it re-checks once", async () => {
    const first = deferred<unknown>();
    const openLockfile = vi.fn().mockReturnValueOnce(first.promise).mockImplementation(() => Promise.resolve(doc(file.rev, "GO")));
    const file = mount(openLockfile);
    await act(async () => button("Open robot")!.click());
    await act(async () => button("Re-check")!.click()); // the check reads r0
    await declare(); // the file is now r0+
    await act(async () => first.resolve(doc("r0", "NO-GO")));
    expect(openLockfile).toHaveBeenCalledTimes(2);
    goMode(container, "Check");
    expect(word()).toBe("GO");
  });

  it("switching to robot B during the automatic re-run shows B, never the re-run's result for A", async () => {
    const first = deferred<unknown>(), rerun = deferred<unknown>();
    const openLockfile = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(rerun.promise);
    let menu: (reply: unknown) => void = () => undefined;
    const file = mount(openLockfile, "r0", { onOpenedFromMenu: (cb: typeof menu) => { menu = cb; return () => undefined; } });
    await act(async () => button("Open robot")!.click());
    await act(async () => button("Re-check")!.click());
    await declare();
    await act(async () => first.resolve(doc("r0", "NO-GO"))); // the re-run for A starts
    expect(openLockfile).toHaveBeenCalledTimes(2);
    await act(async () => menu({ result: picked({ ...doc(file.rev, "GO"), path: "/b.yaml" }) }));
    await act(async () => rerun.resolve(doc(file.rev, "NO-GO")));
    goMode(container, "Check");
    expect(word()).toBe("GO");
  });

  it("no edit during Re-check: exactly one check, no extra run", async () => {
    const openLockfile = vi.fn(() => Promise.resolve(doc("r0", "NO-GO")));
    mount(openLockfile);
    await act(async () => button("Open robot")!.click());
    await act(async () => button("Re-check")!.click());
    expect(openLockfile).toHaveBeenCalledTimes(1);
    expect(word()).toBe("NO-GO");
  });

  it("a file still changing during the re-run is re-checked only once more, then marked with one tap to refresh", async () => {
    const openLockfile = vi.fn().mockResolvedValueOnce(doc("r0", "NO-GO"))
      .mockImplementation(() => { file.rev = `${file.rev}+`; return openLockfile.mock.calls.length > 4 ? new Promise(() => undefined) : Promise.resolve(doc("r1", "NO-GO")); }); // still changing on every read; a loop stalls at 5
    const file = mount(openLockfile);
    await act(async () => button("Open robot")!.click());
    file.rev = "r1"; // written by another tool after the Re-check read the file
    await act(async () => button("Re-check")!.click());
    expect(openLockfile).toHaveBeenCalledTimes(2); // never a loop
    expect(container.textContent).toContain("The robot changed while it was being checked. Press Re-check for the current result.");
  });
});
