import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { DECLARE_FORM, PAGE, PAGE_WITH_SCENE, deferred, mockMatchMedia, picked } from "./App.testkit";
import type { PagePayload } from "../shared/api.gen";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import type { CheckFacePayload } from "./checkFacePayload";
import { setSelectedPartId } from "./scene/selectionStore";

const canvas = vi.hoisted(() => ({ parts: [] as unknown[] }));
vi.mock("./scene/Canvas", () => ({
  Canvas: ({ children, parts }: { children?: React.ReactNode; parts?: unknown[] }) => { canvas.parts = parts ?? []; return <div data-testid="scene-canvas">{children}</div>; },
  useScene: () => ({ meshes: [] }),
}));

let container: HTMLDivElement;
let root: Root;
beforeEach(() => { mockMatchMedia(); setSelectedPartId(null); canvas.parts = []; container = document.createElement("div"); document.body.appendChild(container); });
afterEach(() => { act(() => root.unmount()); container.remove(); Reflect.deleteProperty(window, "lockwire"); });

const GO: CheckFacePayload = { ...X500_CHECK_PAYLOAD, verdict: "GO", robot: "Robot A" };
const doc = (path: string, payload = GO) => ({ opened: true, path, payload, checkPayload: { ...payload, scene: PAGE_WITH_SCENE.scene } });
const word = () => container.querySelector(".check-verdict-word")?.textContent;

function mount(open: () => unknown, page: (c: string) => Promise<PagePayload>, openLockfile = vi.fn()) {
  Object.assign(window, { lockwire: { startFromUrdf: () => Promise.resolve(open()).then(picked), openLockfile, getContractPage: ({ contract }: { contract: string }) => page(contract), getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]) } });
  root = createRoot(container);
  act(() => root.render(<App />));
  return act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Open robot")!.click());
}

describe("opening a robot shows the 3D view before the findings", () => {
  it("while the page is pending the headline reads Checking and never GO; it turns GO once the page lands", async () => {
    const page = deferred<PagePayload>();
    await mount(() => doc("/a.yaml"), () => page.promise);
    expect(word()).toBe("Checking…");
    expect(container.querySelector(".check-headline")?.textContent).not.toMatch(/\bGO\b/);
    await act(async () => page.resolve({ ...PAGE, revision: "r" }));
    expect(word()).toBe("GO");
  });

  it("the 3D view gets the scene from the check reply before the page arrives", async () => {
    await mount(() => doc("/a.yaml"), () => new Promise<PagePayload>(() => {}));
    expect(canvas.parts).toHaveLength(1);
    expect(container.querySelector(".open-face")).toBeNull();
  });

  it("opening robot B after robot A never shows A's verdict or findings", async () => {
    const pageB = deferred<PagePayload>();
    const reopen = vi.fn().mockResolvedValue(doc("/b.yaml", { ...X500_CHECK_PAYLOAD, verdict: "NO-GO", robot: "Robot B" }));
    await mount(() => doc("/a.yaml"), (c) => c === "/a.yaml" ? Promise.resolve({ ...PAGE, revision: "r" }) : pageB.promise, reopen);
    expect(word()).toBe("GO");
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Re-check")!.click());
    expect(reopen).toHaveBeenCalled();
    expect(word()).toBe("Checking…");
    expect(container.querySelector(".finding-card")).toBeNull();
  });
  it("robot A's check reply landing after robot B was opened never draws over B", async () => {
    const slowA = deferred<ReturnType<typeof doc>>();
    const B = { ...doc("/b.yaml", { ...X500_CHECK_PAYLOAD, verdict: "NO-GO", robot: "Robot B" }), checkPayload: { ...X500_CHECK_PAYLOAD, scene: { ...PAGE_WITH_SCENE.scene!, parts: [] } } };
    const reopen = vi.fn().mockReturnValueOnce(slowA.promise).mockResolvedValue(B);
    await mount(() => doc("/a.yaml"), () => Promise.resolve({ ...PAGE, revision: "r" }), reopen);
    const recheck = () => act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Re-check")!.click());
    await recheck(); await recheck(); // the first re-check is slow; the second is robot B's
    await act(async () => slowA.resolve(doc("/a.yaml")));
    expect(word()).toBe("NO-GO");
    expect(container.querySelector(".check-headline")?.textContent).not.toMatch(/\bGO\b/);
  });
});
