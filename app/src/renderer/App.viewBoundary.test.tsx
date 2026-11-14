import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

import { App } from "./App";

vi.mock("./scene/RobotView", () => ({ RobotView: () => { throw new Error("webgl"); } }));
vi.mock("./scene/Canvas", () => ({ Canvas: () => null, useScene: () => ({ meshes: [] }) }));
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("a 3D view that throws leaves the app's own controls on screen with the plain line", () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })) as never;
  Object.assign(window, { lockwire: { getHistory: vi.fn().mockResolvedValue([]) } });
  const host = document.createElement("div");
  act(() => createRoot(host).render(<App />));
  expect(host.textContent).toContain("The 3D view stopped. Your robot file is untouched.");
  expect([...host.querySelectorAll("button")].some((b) => b.textContent === "Open robot")).toBe(true);
});
