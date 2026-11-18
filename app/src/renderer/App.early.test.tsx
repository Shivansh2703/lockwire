import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { DECLARE_FORM, PAGE, PAGE_WITH_SCENE, deferred, mockMatchMedia, picked } from "./App.testkit";
import type { PagePayload } from "../shared/api.gen";
import { X500_CHECK_PAYLOAD } from "./checkFace.fixture";
import type { CheckFacePayload } from "./checkFacePayload";
import { setSelectedPartId } from "./scene/selectionStore";

const canvas = vi.hoisted(() => ({ parts: [] as unknown[], key: "" }));
vi.mock("./scene/Canvas", () => ({
  Canvas: ({ children, parts, sceneKey }: { children?: React.ReactNode; parts?: unknown[]; sceneKey?: string }) => { canvas.parts = parts ?? []; canvas.key = sceneKey ?? ""; return <div data-testid="scene-canvas">{children}</div>; },
  useScene: () => ({ meshes: [] }),
}));

let container: HTMLDivElement;
let root: Root;
beforeEach(() => { mockMatchMedia(); setSelectedPartId(null); canvas.parts = []; container = document.createElement("div"); document.body.appendChild(container); });
afterEach(() => { act(() => root.unmount()); container.remove(); Reflect.deleteProperty(window, "lockwire"); });

const GO: CheckFacePayload = { ...X500_CHECK_PAYLOAD, verdict: "GO", robot: "Robot A" };
const doc = (path: string, payload = GO) => ({ opened: true, path, payload, checkPayload: { ...payload, scene: PAGE_WITH_SCENE.scene } });
const word = () => container.querySelector(".check-verdict-word")?.textContent;

const menu: { cb?: (r: unknown) => void } = {};
function mount(open: () => unknown, page: (c: string) => Promise<PagePayload>, openLockfile = vi.fn()) {
  Object.assign(window, { lockwire: { startFromUrdf: () => Promise.resolve(open()).then(picked), openLockfile, getContractPage: ({ contract }: { contract: string }) => page(contract), getDeclareForm: vi.fn().mockResolvedValue(DECLARE_FORM), getHistory: vi.fn().mockResolvedValue([]), onOpenedFromMenu: (cb: (r: unknown) => void) => { menu.cb = cb; return () => undefined; } } });
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
  it("switching to a robot whose check reply carries no scene shows Checking, never the start screen", async () => {
    const pageB = deferred<PagePayload>();
    const B = { ...doc("/b.yaml", { ...X500_CHECK_PAYLOAD, verdict: "NO-GO", robot: "Robot B" }), checkPayload: { ...X500_CHECK_PAYLOAD, scene: undefined } };
    await mount(() => doc("/a.yaml"), (c) => c === "/a.yaml" ? Promise.resolve({ ...PAGE, revision: "r" }) : pageB.promise, vi.fn().mockResolvedValue(B));
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Re-check")!.click());
    expect(container.querySelector(".open-face")).toBeNull();
    expect(word()).toBe("Checking…");
    const pending = canvas.key; // the view mounted before the page has no skeleton yet: it must rebuild when the page lands
    await act(async () => pageB.resolve({ ...PAGE, revision: "r2" }));
    expect(canvas.key).not.toBe(pending);
  });
  it("a switch overtaken by a file the engine refuses shows the refusal, not Checking forever", async () => {
    const pageB = deferred<PagePayload>();
    const B = { ...doc("/b.yaml", { ...X500_CHECK_PAYLOAD, verdict: "NO-GO", robot: "Robot B" }), checkPayload: { ...X500_CHECK_PAYLOAD, scene: undefined } };
    await mount(() => doc("/a.yaml"), (c) => c === "/a.yaml" ? Promise.resolve({ ...PAGE, revision: "r" }) : pageB.promise, vi.fn().mockResolvedValue(B));
    await act(async () => [...container.querySelectorAll("button")].find((b) => b.textContent === "Re-check")!.click());
    expect(word()).toBe("Checking…");
    await act(async () => menu.cb!({ result: { started: false, opened: { opened: true, path: "/c.yaml", error: "not a robot model" } } }));
    await act(async () => pageB.resolve({ ...PAGE, revision: "r2" }));
    expect(word()).not.toBe("Checking…");
    expect(container.textContent).toContain("not a robot model");
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
