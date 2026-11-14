import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RootBoundary, ViewBoundary } from "./ErrorBoundary";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
let boom = true;
const Bomb = () => { if (boom) throw new Error("webgl"); return <p>view ok</p>; };
const host = document.createElement("div");
const root = createRoot(host);
const show = (el: React.ReactNode) => act(() => root.render(el));
afterEach(() => { boom = true; vi.restoreAllMocks(); });

describe("error boundaries", () => {
  it("a throwing child shows one plain line, not a blank tree", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    show(<RootBoundary><Bomb /></RootBoundary>);
    expect(host.textContent).toContain("Lockwire stopped. Your robot file is untouched.");
    expect(host.querySelector("button")?.textContent).toBe("Reload Lockwire");
  });
  it("a failed view keeps its siblings and Reload the view remounts it", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    show(<div><ViewBoundary><Bomb /></ViewBoundary><p>panel</p></div>);
    expect(host.textContent).toContain("The 3D view stopped.");
    expect(host.textContent).toContain("panel");
    boom = false;
    act(() => host.querySelector("button")!.click());
    expect(host.textContent).toContain("view ok");
  });
});
