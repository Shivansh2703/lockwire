import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { expect, it, vi } from "vitest";

vi.mock("./App", () => ({ App: () => { throw new Error("boom"); } }));
import { Root } from "./Root";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

it("the app root shows one plain line and Reload Lockwire when the app throws", () => {
  vi.spyOn(console, "error").mockImplementation(() => {});
  const host = document.createElement("div");
  act(() => createRoot(host).render(<Root />));
  expect(host.textContent).toContain("Lockwire stopped. Your robot file is untouched.");
  expect(host.querySelector("button")?.textContent).toBe("Reload Lockwire");
});
